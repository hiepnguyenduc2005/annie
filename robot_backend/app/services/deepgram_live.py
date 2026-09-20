"""One bounded listening turn using Deepgram's streaming transcription API."""

import asyncio
import json
from contextlib import suppress
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .deepgram import SpeechError


class Transcript:
    def __init__(self):
        self.parts: list[str] = []
        self.seen: set[tuple] = set()
        self.heard_speech = False

    def consume(self, message: str) -> str | None:
        try:
            event = json.loads(message)
            if event.get("type") == "Error":
                raise SpeechError("Live transcription failed")
            if event.get("type") == "Results":
                text = event["channel"]["alternatives"][0]["transcript"]
                if not isinstance(text, str):
                    raise ValueError
                self.heard_speech |= bool(text.strip())
                identity = (event.get("start"), event.get("duration"))
                if (
                    event.get("is_final") is True
                    and text.strip()
                    and identity not in self.seen
                ):
                    self.seen.add(identity)
                    self.parts.append(text.strip())
                if sum(map(len, self.parts)) > 8000 or len(self.seen) > 256:
                    raise SpeechError(
                        "Spoken turn is too long; please try a shorter turn", 422
                    )
                if event.get("speech_final") is True and self.parts:
                    return " ".join(self.parts)
            elif event.get("type") == "UtteranceEnd" and self.parts:
                return " ".join(self.parts)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise SpeechError("Live transcription returned an invalid result") from None
        return None


class DeepgramLive:
    def __init__(self, settings):
        self.settings = settings

    async def listen(
        self, packets: asyncio.Queue, ready, transcript: Transcript
    ) -> str:
        key = self.settings.deepgram_api_key.get_secret_value()
        if not key:
            raise SpeechError("Set DEEPGRAM_API_KEY to enable speech", 503)
        url = urlsplit(self.settings.deepgram_base_url)
        query = urlencode(
            {
                "model": self.settings.deepgram_stt_model,
                "language": self.settings.deepgram_language,
                "encoding": "linear16",
                "sample_rate": 16000,
                "channels": 1,
                "interim_results": "true",
                "smart_format": "true",
                "endpointing": self.settings.audio_endpointing_ms,
                "utterance_end_ms": 1200,
            }
        )
        endpoint = urlunsplit(
            (
                "wss" if url.scheme == "https" else "ws",
                url.netloc,
                url.path + "/v1/listen",
                query,
                "",
            )
        )
        try:
            async with connect(
                endpoint,
                additional_headers={"Authorization": "Token " + key},
                open_timeout=10,
                close_timeout=2,
                max_size=1024 * 1024,
                max_queue=16,
                compression=None,
                proxy=None,
            ) as socket:

                async def send():
                    while True:
                        try:
                            packet = await asyncio.wait_for(packets.get(), 4)
                        except asyncio.TimeoutError:
                            await socket.send('{"type":"KeepAlive"}')
                        else:
                            await socket.send(packet)

                async def receive():
                    async for message in socket:
                        if not isinstance(message, str):
                            raise SpeechError("Unexpected live transcription audio")
                        text = transcript.consume(message)
                        if text:
                            return text
                    raise SpeechError("Live transcription disconnected")

                sender = asyncio.create_task(send())
                receiver = asyncio.create_task(receive())
                try:
                    await ready()
                    finished, _ = await asyncio.wait(
                        [sender, receiver], return_when=asyncio.FIRST_COMPLETED
                    )
                    if sender in finished:
                        await sender
                    return await receiver
                finally:
                    for task in (sender, receiver):
                        task.cancel()
                    for task in (sender, receiver):
                        with suppress(
                            asyncio.CancelledError,
                            SpeechError,
                            WebSocketException,
                            OSError,
                        ):
                            await task
        except InvalidStatus as exc:
            raise SpeechError(
                f"Deepgram live connection rejected (HTTP {exc.response.status_code})"
            ) from None
        except (WebSocketException, OSError, asyncio.TimeoutError):
            raise SpeechError("Deepgram live transcription is unavailable") from None
