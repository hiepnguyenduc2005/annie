"""Local offline speech recognition adapter for the simulated dog.

Wraps faster-whisper (tiny.en by default) for CPU transcription of bounded
16-bit PCM WAV clips. The model downloads once to `.cache/models/whisper`
inside the repository and is never shared off the machine. No cloud calls,
accounts, or network access happen at transcribe time; the only network use
is the initial public open-source model download.

Outputs are evidence-based: `text` and per-segment `avg_logprob` /
`no_speech_prob` are the raw Whisper numbers, passed through unchanged and
labeled NOT calibrated confidence estimates. `latency_ms` is measured
wall time of the transcribe call only, excluding model load (callers should
warm the model first; see docs/LOCAL_STT.md).
"""
from __future__ import annotations

import io
import logging
import math
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("annie.simulation.local_stt")

DEFAULT_MODEL_SIZE = "tiny.en"
DEFAULT_CACHE_DIR = Path(".cache/models/whisper")
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
LANGUAGE = "en"
CPU_THREADS = 4

# Same audio bounds as robot_backend/app/brain/audio.py so simulated clips
# accepted there are accepted here (and vice versa).
MAX_WAV_BYTES = 4_000_000
MAX_DURATION_S = 20.0
MIN_SAMPLE_RATE_HZ = 8_000
MAX_SAMPLE_RATE_HZ = 48_000


class LocalSTTError(Exception):
    """Raised when a clip cannot be validated or transcribed."""


@dataclass
class Segment:
    start: float
    end: float
    text: str
    # Raw Whisper quality signals. NOT calibrated confidence: they have not
    # been evaluated against ground truth on this robot, so do not treat
    # them as probabilities of correctness.
    avg_logprob: float
    no_speech_prob: float


@dataclass
class Transcription:
    text: str
    language: str
    utterance_id: str | None
    duration_s: float
    latency_ms: float
    model: str
    segments: list[Segment] = field(default_factory=list)


def _read_wav(source: bytes, *, what: str = "WAV") -> tuple[float, float]:
    """Validate bounded 16-bit PCM WAV bytes; return (duration_s, frames)."""
    if not source:
        raise LocalSTTError(f"{what} is empty.")
    if len(source) > MAX_WAV_BYTES:
        raise LocalSTTError(f"{what} exceeds {MAX_WAV_BYTES} decoded bytes.")
    try:
        with wave.open(io.BytesIO(source), "rb") as clip:
            channels = clip.getnchannels()
            width = clip.getsampwidth()
            rate = clip.getframerate()
            frames = clip.getnframes()
            if channels not in (1, 2):
                raise LocalSTTError(f"{what} must be mono or stereo.")
            if width != 2 or clip.getcomptype() != "NONE":
                raise LocalSTTError(f"{what} must be 16-bit PCM.")
            if not MIN_SAMPLE_RATE_HZ <= rate <= MAX_SAMPLE_RATE_HZ:
                raise LocalSTTError(
                    f"{what} sample rate must be {MIN_SAMPLE_RATE_HZ}"
                    f"-{MAX_SAMPLE_RATE_HZ} Hz.")
            if frames < 1 or frames / rate > MAX_DURATION_S:
                raise LocalSTTError(
                    f"{what} duration must be within {MAX_DURATION_S:g} seconds.")
            samples = clip.readframes(frames)
            if len(samples) != frames * channels * width:
                raise LocalSTTError(f"{what} data chunk is truncated.")
    except LocalSTTError:
        raise
    except (wave.Error, EOFError, OSError, ValueError):
        raise LocalSTTError(f"Invalid {what} encoding.") from None
    return frames / rate, frames


class LocalSTTAdapter:
    """Serial, bounded, offline Whisper transcription of PCM WAV clips."""

    def __init__(self, model_size=DEFAULT_MODEL_SIZE, *,
                 cache_dir=DEFAULT_CACHE_DIR, device=DEFAULT_DEVICE,
                 compute_type=DEFAULT_COMPUTE_TYPE):
        if not isinstance(model_size, str) or not model_size.strip():
            raise LocalSTTError("model_size must be a non-empty string.")
        self.model_size = model_size.strip()
        self.cache_dir = Path(cache_dir)
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self.model_size, device=self.device,
                compute_type=self.compute_type, download_root=str(self.cache_dir),
                cpu_threads=CPU_THREADS)
        return self._model

    def warm(self):
        """Load the model and run a tiny warm-up decode so first real use is fast."""
        model = self._load_model()
        rate = 16_000
        segments, _ = model.transcribe(  # warm-up decode on 1 s of silence
            _float32_from_pcm16(b"\x00\x00" * rate),
            language=LANGUAGE, beam_size=1)
        list(segments)  # faster-whisper decodes lazily.

    def transcribe(self, source, *, utterance_id=None):
        """Transcribe WAV bytes or a file path to a Transcription result.

        `source` is a Path/str file path or raw WAV bytes. `utterance_id`
        is a stable caller identifier passed through unchanged on the result.
        Raises LocalSTTError on invalid audio or decode failure.
        """
        if isinstance(source, (str, Path)):
            try:
                audio_bytes = Path(source).read_bytes()
            except OSError as exc:
                raise LocalSTTError(f"Cannot read WAV file: {source}") from exc
        elif isinstance(source, (bytes, bytearray)):
            audio_bytes = bytes(source)
        else:
            raise LocalSTTError("source must be a path or WAV bytes.")
        duration_s, _frames = _read_wav(audio_bytes)
        model = self._load_model()
        started = time.perf_counter()
        try:
            segments_iter, info = model.transcribe(
                io.BytesIO(audio_bytes), language=LANGUAGE, beam_size=1,
                vad_filter=False)
            segments = [
                Segment(start=seg.start, end=seg.end, text=seg.text.strip(),
                        avg_logprob=seg.avg_logprob,
                        no_speech_prob=seg.no_speech_prob)
                for seg in segments_iter
            ]
        except LocalSTTError:
            raise
        except Exception as exc:
            raise LocalSTTError(f"Transcription failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise LocalSTTError("Latency accounting failed.")
        return Transcription(
            text=" ".join(seg.text for seg in segments if seg.text).strip(),
            language=info.language or LANGUAGE,
            utterance_id=utterance_id,
            duration_s=duration_s,
            latency_ms=latency_ms,
            model=self.model_size,
            segments=segments,
        )


def _float32_from_pcm16(raw: bytes):
    import numpy as np

    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0


def main(argv=None):
    import argparse

    global LOG
    parser = argparse.ArgumentParser(
        description="Local Whisper STT for simulation WAV clips (offline at run time).")
    parser.add_argument("wav", help="Path to a 16-bit PCM WAV clip")
    parser.add_argument("--utterance-id", default=None,
                        help="Stable identifier passed through to the result")
    parser.add_argument("--model", default=DEFAULT_MODEL_SIZE)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    adapter = LocalSTTAdapter(args.model, cache_dir=args.cache_dir)
    result = adapter.transcribe(args.wav, utterance_id=args.utterance_id)
    LOG.info("text: %s", result.text or "(none)")
    for seg in result.segments:
        LOG.info(
            "segment %.2f-%.2fs avg_logprob=%.3f no_speech_prob=%.3f "
            "(NOT calibrated confidence)", seg.start, seg.end,
            seg.avg_logprob, seg.no_speech_prob)
    LOG.info("latency_ms=%.0f model=%s language=%s duration_s=%.2f",
             result.latency_ms, result.model, result.language, result.duration_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
