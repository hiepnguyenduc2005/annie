"""Local offline speech synthesis adapter for the simulated dog.

Uses the macOS built-in `say` binary to render bounded text to a 22.05 kHz
16-bit mono WAV file under `.data/simulation/speech/`. No network access and
no provider accounts are involved. The adapter never plays audio; a later
browser viewer owns playback and sends an explicit play acknowledgment.

Receipt status values are evidence-based: `synthesized` only after the
`say` process exited 0 and the output file passed a RIFF/WAVE header check;
`failed` for validation, spawn, timeout, or non-zero exits. Durations are
derived from the parsed header, not estimated from text length.
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
from pathlib import Path
from uuid import UUID, uuid4

LOG = logging.getLogger("annie.simulation.speech")

MAX_TEXT_CHARS = 2000
TIMEOUT_SECONDS = 30.0
SAMPLE_RATE_HZ = 22050
CHANNELS = 1
BYTES_PER_SAMPLE = 2

_RIFF = b"RIFF"
_WAVE = b"WAVE"
_FMT = b"fmt "
_DATA = b"data"
_PCM = 1


class SpeechError(Exception):
    """Raised when speech synthesis cannot produce a valid bounded clip."""


class SpeechAdapter:
    """Serial, bounded, offline text-to-speech file renderer (`say -o`)."""

    def __init__(self, output_dir=".data/simulation/speech", *, say_path="say",
                 timeout=TIMEOUT_SECONDS, max_chars=MAX_TEXT_CHARS):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.output_dir = Path(output_dir)
        self.say_path = say_path
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)
        self._lock = asyncio.Lock()

    def validate_text(self, text):
        if not isinstance(text, str):
            raise SpeechError("Speech text must be a string.")
        stripped = text.strip()
        if not stripped:
            raise SpeechError("Speech text must not be empty.")
        if len(stripped) > self.max_chars:
            raise SpeechError(
                f"Speech text exceeds {self.max_chars} characters "
                f"(got {len(stripped)}).")
        return stripped

    async def speak(self, text, command_id):
        """Render `text` to one WAV file and return clip metadata.

        Returns a receipt-style dict with status `synthesized`; playback is
        explicitly not performed and remains awaiting browser acknowledgment.
        Raises SpeechError on invalid input, spawn failure, timeout, non-zero
        exit, or an unreadable/invalid WAV header. Calls are serialized so at
        most one `say` process runs at a time.
        """
        clean = self.validate_text(text)
        if not isinstance(command_id, str):
            raise SpeechError("command_id must be a canonical UUID string.")
        try:
            canonical = str(UUID(hex=command_id))
        except ValueError as exc:
            raise SpeechError("command_id must be a canonical UUID string.") from exc
        if canonical != command_id:
            raise SpeechError("command_id must be a canonical UUID string.")
        async with self._lock:
            return await self._synthesize(clean, canonical)

    async def _spawn(self, argv):
        # Single injection point for tests; no shell, no environment reading.
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _synthesize(self, text, command_id):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        clip_path = self.output_dir / f"{command_id}.wav"
        # Write to a unique temp path first so a crash or timeout never leaves
        # a partial file at the final clip path.
        temp_path = clip_path.with_name(f".{uuid4().hex}.part")
        try:
            process = await self._spawn([
                self.say_path,
                "--file-format=WAVE",
                f"--data-format=LEI16@{SAMPLE_RATE_HZ}",
                "-o", str(temp_path),
                "--",
                text,
            ])
            try:
                returncode = await asyncio.wait_for(process.wait(), timeout=self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise SpeechError(
                    f"say timed out after {self.timeout:g}s (command {command_id}).")
            if returncode != 0:
                raise SpeechError(
                    f"say exited with status {returncode} (command {command_id}).")
            sample_count = parse_wav_header(temp_path.read_bytes())
            duration = sample_count / SAMPLE_RATE_HZ
            temp_path.replace(clip_path)
            return {
                "command_id": command_id,
                "status": "synthesized",
                "file_path": str(clip_path),
                "duration_s": duration,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "channels": CHANNELS,
                "played": False,
            }
        except SpeechError:
            temp_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise SpeechError(f"say could not be started or read: {exc}") from exc


def parse_wav_header(blob):
    """Return PCM sample-frame count from a RIFF/WAVE header blob.

    Validates the canonical layout emitted by `say --file-format=WAVE`:
    RIFF/WAVE, a 16-byte PCM `fmt ` chunk (mono, 22050 Hz, 16-bit), then a
    `data` chunk. Raises SpeechError on anything else. Length fields are
    trusted only as far as the actual blob length; no samples are decoded.
    """
    def need(offset, size, what):
        if offset + size > len(blob):
            raise SpeechError(f"WAV file truncated in {what}.")
        return blob[offset:offset + size]

    if need(0, 12, "RIFF header")[:4] != _RIFF or \
            need(8, 4, "RIFF header") != _WAVE:
        raise SpeechError("Not a canonical RIFF/WAVE file.")
    offset = 12
    fmt_seen = False
    while offset + 8 <= len(blob):
        chunk_id = need(offset, 4, "chunk id")
        (chunk_size,) = struct.unpack("<I", need(offset + 4, 4, "chunk size"))
        body_offset = offset + 8
        if chunk_id == _FMT:
            body = need(body_offset, 16, "fmt chunk")
            (audio_format, channels, rate, _byte_rate, _align, bits) = \
                struct.unpack("<HHIIHH", body)
            if audio_format != _PCM or channels != CHANNELS or \
                    rate != SAMPLE_RATE_HZ or bits != BYTES_PER_SAMPLE * 8:
                raise SpeechError("Unexpected WAV format; expected PCM mono 16-bit 22050 Hz.")
            fmt_seen = True
        elif chunk_id == _DATA:
            if not fmt_seen:
                raise SpeechError("WAV data chunk appears before fmt chunk.")
            available = len(blob) - body_offset
            if chunk_size > available:
                raise SpeechError("WAV data chunk extends past end of file.")
            if chunk_size % BYTES_PER_SAMPLE:
                raise SpeechError("WAV data size is not a whole number of 16-bit samples.")
            return chunk_size // BYTES_PER_SAMPLE
        offset = body_offset + chunk_size + (chunk_size & 1)
    raise SpeechError("WAV data chunk not found.")
