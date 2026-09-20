"""Cloud voice for the family-facing conversation: ElevenLabs speaks, Deepgram hears; local fallback always.

Division of labour (team decision 2026-09-20): Deepgram turns Jeanine's spoken reply
into text, Annie's brain/memory decides, ElevenLabs turns the response into a lifelike
voice. Neither service ever receives camera frames; audio is the utterance captured on
the host microphone after the VAD, and text is the line Annie is about to say. When a
key is missing, or a request fails or exceeds its short timeout, the same call falls
back to macOS `say` / local Whisper so a network drop cannot stall the robot mid-demo.
Playback and capture use the host's default audio devices (the operator's AirPods).

Keys: `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` (default: Rachel), `DEEPGRAM_API_KEY`,
from the ignored `.env` only. Nothing here logs audio, transcripts or keys.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel"


def elevenlabs_tts(text: str, *, api_key: str, voice_id: str = DEFAULT_VOICE, model_id: str = "eleven_flash_v2_5",
                   timeout_s: float = 8.0, post=None) -> bytes | None:
    """Text -> MP3 bytes, or None on any failure (caller falls back)."""
    import httpx
    body = {"text": text[:500], "model_id": model_id, "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}}
    headers = {"xi-api-key": api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    try:
        if post is not None:
            return post(ELEVEN_URL.format(voice=voice_id), body, headers, timeout_s)
        with httpx.Client(timeout=timeout_s, trust_env=False) as client:
            r = client.post(ELEVEN_URL.format(voice=voice_id), json=body, headers=headers,
                            params={"output_format": "mp3_22050_32"})
            if r.status_code != 200 or not r.content:
                return None
            return r.content
    except Exception:
        return None


def deepgram_transcribe(wav_bytes: bytes, *, api_key: str, timeout_s: float = 6.0, post=None) -> str | None:
    """WAV bytes -> transcript text, or None on any failure (caller falls back to local Whisper)."""
    import httpx
    params = {"model": "nova-3", "smart_format": "true", "language": "en"}
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"}
    try:
        if post is not None:
            data = post(DEEPGRAM_URL, wav_bytes, headers, timeout_s)
        else:
            with httpx.Client(timeout=timeout_s, trust_env=False) as client:
                r = client.post(DEEPGRAM_URL, params=params, headers=headers, content=wav_bytes)
                if r.status_code != 200:
                    return None
                data = r.json()
        alt = data["results"]["channels"][0]["alternatives"][0]
        text = (alt.get("transcript") or "").strip()
        return text or None
    except Exception:
        return None


def play_audio_bytes(data: bytes, suffix=".mp3", timeout_s=60.0) -> bool:
    """Play on the host's default output (afplay on macOS); blocks until done."""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(data)
            path = fh.name
        rc = subprocess.run(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s).returncode
        os.unlink(path)
        return rc == 0
    except Exception:
        return False


class CloudVoice:
    """speak(text) and transcribe(wav) with cloud first, local fallback; counts what happened."""

    def __init__(self, *, eleven_key=None, eleven_voice=None, deepgram_key=None, local_speak=None, local_transcribe=None,
                 player=play_audio_bytes, tts=elevenlabs_tts, stt=deepgram_transcribe):
        self.eleven_key = eleven_key if eleven_key is not None else os.environ.get("ELEVENLABS_API_KEY") or None
        self.eleven_voice = eleven_voice or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE
        self.deepgram_key = deepgram_key if deepgram_key is not None else os.environ.get("DEEPGRAM_API_KEY") or None
        self.local_speak, self.local_transcribe = local_speak, local_transcribe
        self.player, self.tts, self.stt = player, tts, stt
        self.stats = {"tts_cloud": 0, "tts_local": 0, "stt_cloud": 0, "stt_local": 0, "tts_ms": None, "stt_ms": None}

    @property
    def enabled(self):
        return {"elevenlabs": bool(self.eleven_key), "deepgram": bool(self.deepgram_key)}

    def speak(self, text: str) -> bool:
        if self.eleven_key:
            t0 = time.perf_counter()
            audio = self.tts(text, api_key=self.eleven_key, voice_id=self.eleven_voice)
            self.stats["tts_ms"] = round((time.perf_counter() - t0) * 1000)
            if audio and self.player(audio):
                self.stats["tts_cloud"] += 1
                return True
        self.stats["tts_local"] += 1
        return bool(self.local_speak(text)) if self.local_speak else False

    def transcribe(self, wav_bytes: bytes) -> str | None:
        if self.deepgram_key:
            t0 = time.perf_counter()
            text = self.stt(wav_bytes, api_key=self.deepgram_key)
            self.stats["stt_ms"] = round((time.perf_counter() - t0) * 1000)
            if text:
                self.stats["stt_cloud"] += 1
                return text
        self.stats["stt_local"] += 1
        return self.local_transcribe(wav_bytes) if self.local_transcribe else None
