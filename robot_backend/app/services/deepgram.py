"""Deepgram REST speech adapters. Only current audio and spoken reply leave here."""

import base64
import io
import wave
from typing import Protocol

import httpx

from ..config import Settings


class SpeechError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class SpeechClient(Protocol):
    async def transcribe(self, audio: bytes, mime: str) -> str: ...
    async def synthesize(self, text: str, *, sample_rate: int = 24000) -> str: ...


class DeepgramClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def _request(self, path: str, *, params: dict, limit: int, **kwargs) -> bytes:
        key = self.settings.deepgram_api_key.get_secret_value()
        if not key:
            raise SpeechError("Set DEEPGRAM_API_KEY to enable speech", 503)
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = "Token " + key
        try:
            async with self.client.stream(
                "POST",
                self.settings.deepgram_base_url + path,
                params=params,
                headers=headers,
                timeout=self.settings.deepgram_request_timeout,
                **kwargs,
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise SpeechError("Deepgram response exceeded the size limit")
                return bytes(data)
        except httpx.HTTPError:
            # Never echo response bodies, credentials or resident media.
            raise SpeechError(
                "Deepgram is unavailable or rejected the speech request"
            ) from None

    async def transcribe(self, audio: bytes, mime: str) -> str:
        import json

        body = await self._request(
            "/v1/listen",
            params={
                "model": self.settings.deepgram_stt_model,
                "language": self.settings.deepgram_language,
                "smart_format": "true",
            },
            headers={"Content-Type": mime},
            content=audio,
            limit=1024 * 1024,
        )
        try:
            result = json.loads(body)
            text = result["results"]["channels"][0]["alternatives"][0]["transcript"]
            if not isinstance(text, str) or len(text) > 8000:
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise SpeechError("Deepgram returned an invalid transcription") from None
        if not text.strip():
            raise SpeechError("No speech was recognized; please try again", 422)
        return text.strip()

    async def synthesize(self, text: str, *, sample_rate: int = 24000) -> str:
        if sample_rate not in {16000, 24000}:
            raise SpeechError("Unsupported speech sample rate")
        if not text.strip() or len(text) > 2000:
            raise SpeechError(
                "Spoken reply is empty or exceeds the speech length limit"
            )
        data = await self._request(
            "/v1/speak",
            params={
                "model": self.settings.deepgram_tts_model,
                "encoding": "linear16",
                "container": "none",
                "sample_rate": str(sample_rate),
            },
            json={"text": text},
            limit=32 * 1024 * 1024,
        )
        # Request raw PCM and write a finalized WAV ourselves. Streaming WAV
        # headers may declare placeholder lengths that strict readers reject.
        if not data or len(data) % 2 or data.startswith(b"RIFF"):
            raise SpeechError("Deepgram returned invalid PCM audio")
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(sample_rate)
            target.writeframes(data)
        return base64.b64encode(output.getvalue()).decode("ascii")
