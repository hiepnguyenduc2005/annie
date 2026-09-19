"""Tests for the local Whisper STT adapter. The model is always mocked."""
from __future__ import annotations

import dataclasses
import io
import wave

import pytest

from robot.simulation.local_stt import (
    DEFAULT_CACHE_DIR,
    LocalSTTAdapter,
    LocalSTTError,
    Segment,
    Transcription,
)


def pcm_wav(seconds=1.0, rate=16_000, channels=1, freq=440.0, amplitude=8000):
    import math

    total = int(seconds * rate)
    frames = bytearray()
    for i in range(total):
        sample = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        frame = sample.to_bytes(2, "little", signed=True)
        frames += frame * channels
    buf = io.BytesIO()
    with wave.open(buf, "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(bytes(frames))
    return buf.getvalue()


class FakeModel:
    def __init__(self, text="hi annie", logprob=-0.21, no_speech=0.02):
        self.text = text
        self.logprob = logprob
        self.no_speech = no_speech
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)

        class Info:
            language = "en"

        class Seg:
            start = 0.0
            end = 1.0
            text = f" {self.text}"
            avg_logprob = self.logprob
            no_speech_prob = self.no_speech

        def gen():
            yield Seg()

        return gen(), Info()


def make_adapter(monkeypatch, model=None):
    adapter = LocalSTTAdapter(cache_dir=DEFAULT_CACHE_DIR)
    adapter._model = model if model is not None else FakeModel()
    return adapter


def test_passes_through_text_and_raw_quality_signals():
    model = FakeModel(text="okay annie", logprob=-0.35, no_speech=0.11)
    adapter = make_adapter(None, model)
    result = adapter.transcribe(pcm_wav(), utterance_id="u-123")
    assert isinstance(result, Transcription)
    assert result.text == "okay annie"
    assert result.utterance_id == "u-123"
    assert result.language == "en"
    assert result.model == "tiny.en"
    assert result.duration_s == pytest.approx(1.0)
    assert result.latency_ms >= 0.0
    assert result.segments[0].avg_logprob == pytest.approx(-0.35)
    assert result.segments[0].no_speech_prob == pytest.approx(0.11)


def test_language_is_forced_english():
    model = FakeModel()
    adapter = make_adapter(None, model)
    adapter.transcribe(pcm_wav())
    assert model.calls[0]["language"] == "en"


def test_accepts_path_or_bytes(tmp_path):
    adapter = make_adapter(None)
    path = tmp_path / "clip.wav"
    path.write_bytes(pcm_wav())
    by_path = adapter.transcribe(str(path))
    by_bytes = adapter.transcribe(pcm_wav())
    assert by_path.text == by_bytes.text == "hi annie"


def test_rejects_invalid_and_oversized_audio():
    adapter = make_adapter(None)
    with pytest.raises(LocalSTTError):
        adapter.transcribe(b"not a wav")
    with pytest.raises(LocalSTTError):
        adapter.transcribe(b"")

    adapter2 = make_adapter(None)
    import struct

    # A data chunk claiming more bytes than the file holds is truncated.
    raw = bytearray(pcm_wav(seconds=0.1))
    (data_pos,) = [i for i in range(len(raw) - 8)
                   if raw[i:i + 4] == b"data"][:1]
    struct.pack_into("<I", raw, data_pos + 4, len(raw) * 4)
    with pytest.raises(LocalSTTError):
        adapter2.transcribe(bytes(raw))
    # A valid WAV padded past the byte bound is rejected outright.
    from robot.simulation.local_stt import MAX_WAV_BYTES

    padded = bytearray(pcm_wav(seconds=0.1))
    pad_len = MAX_WAV_BYTES + 1 - len(padded) - 4  # 4 bytes for JUNK
    pad = b"JUNK" + (pad_len * b"\x00")
    padded += pad
    with pytest.raises(LocalSTTError):
        make_adapter(None).transcribe(bytes(padded))


def test_rejects_out_of_bounds_sample_rate_and_duration():
    adapter = make_adapter(None)
    with pytest.raises(LocalSTTError):
        adapter.transcribe(pcm_wav(rate=4_000))
    with pytest.raises(LocalSTTError):
        adapter.transcribe(pcm_wav(seconds=21.0))


def test_rejects_non_wav_source_types():
    adapter = make_adapter(None)
    with pytest.raises(LocalSTTError):
        adapter.transcribe(12345)


def test_missing_file_raises_bounded_error(tmp_path):
    adapter = make_adapter(None)
    with pytest.raises(LocalSTTError):
        adapter.transcribe(tmp_path / "missing.wav")


def test_transcription_is_dataclass_with_segment_fields():
    result = make_adapter(None).transcribe(pcm_wav())
    assert dataclasses.is_dataclass(result)
    seg = result.segments[0]
    assert dataclasses.is_dataclass(seg)
    assert isinstance(seg, Segment)
    assert seg.text == "hi annie"


def test_decode_failure_is_wrapped():
    class Boom(FakeModel):
        def transcribe(self, audio, **kwargs):
            raise RuntimeError("ct2 exploded")

    adapter = make_adapter(None, Boom())
    with pytest.raises(LocalSTTError, match="Transcription failed"):
        adapter.transcribe(pcm_wav())
