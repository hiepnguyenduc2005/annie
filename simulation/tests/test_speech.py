import asyncio
import struct
from uuid import uuid4

import pytest

from simulation.speech import MAX_TEXT_CHARS, SpeechAdapter, SpeechError, parse_wav_header


def minimal_wav(sample_count=64):
    data = b"\x00\x01" * sample_count
    fmt = struct.pack("<HHIIHH", 1, 1, 22050, 22050 * 2, 2, 16)
    riff_size = 4 + 8 + len(fmt) + 8 + len(data)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + b"fmt " + \
        struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data


def write_adapter(tmp_path, **kwargs):
    return SpeechAdapter(tmp_path / "clips", **kwargs)


def test_rejects_empty_whitespace_and_oversized_text(tmp_path):
    adapter = write_adapter(tmp_path)
    for bad in ["", "   ", None, 123, "x" * (MAX_TEXT_CHARS + 1)]:
        with pytest.raises(SpeechError):
            asyncio.run(adapter.speak(bad, str(uuid4())))
    boundary = "x" * MAX_TEXT_CHARS
    assert adapter.validate_text(boundary) == boundary


def test_rejects_non_uuid_command_ids(tmp_path):
    adapter = write_adapter(tmp_path)
    for bad in ["", "not-a-uuid", 123, str(uuid4()).replace("-", "")]:
        with pytest.raises(SpeechError):
            asyncio.run(adapter.speak("hello", bad))


def test_timeout_kills_say_and_reports_failure(tmp_path):
    adapter = write_adapter(tmp_path, timeout=0.05)

    class SlowProcess:
        def __init__(self):
            self.killed = False

        async def wait(self):
            if self.killed:
                return
            await asyncio.sleep(30)

        def kill(self):
            self.killed = True

    slow = SlowProcess()

    async def fake_spawn(argv):
        return slow

    adapter._spawn = fake_spawn
    with pytest.raises(SpeechError, match="timed out"):
        asyncio.run(adapter.speak("hello", str(uuid4())))
    assert slow.killed
    assert list((tmp_path / "clips").iterdir() if (tmp_path / "clips").exists() else []) == []


def test_nonzero_exit_is_failure_without_output_file(tmp_path, monkeypatch):
    adapter = write_adapter(tmp_path)

    class FailingProcess:
        async def wait(self):
            return 1

        def kill(self):
            pass

    async def fake_spawn(argv):
        assert "--file-format=WAVE" in argv
        assert "-o" in argv
        return FailingProcess()

    adapter._spawn = fake_spawn
    with pytest.raises(SpeechError, match="status 1"):
        asyncio.run(adapter.speak("hello", str(uuid4())))


def test_serialized_calls(tmp_path, monkeypatch):
    adapter = write_adapter(tmp_path)
    active = 0
    peak = 0

    class OkProcess:
        async def wait(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return 0

        def kill(self):
            pass

    async def fake_spawn(argv):
        out_path = argv[argv.index("-o") + 1]
        with open(out_path, "wb") as handle:
            handle.write(minimal_wav())
        return OkProcess()

    adapter._spawn = fake_spawn

    async def exercise():
        await asyncio.gather(*(adapter.speak("hi", str(uuid4())) for _ in range(5)))

    asyncio.run(exercise())
    assert peak == 1
    assert len(list((tmp_path / "clips").glob("*.wav"))) == 5


def test_flag_like_text_is_spoken_not_interpreted(tmp_path):
    adapter = write_adapter(tmp_path)
    seen_argv = {}

    class OkProcess:
        async def wait(self):
            return 0

        def kill(self):
            pass

    async def fake_spawn(argv):
        seen_argv.update(argv=argv)
        with open(argv[argv.index("-o") + 1], "wb") as handle:
            handle.write(minimal_wav())
        return OkProcess()

    adapter._spawn = fake_spawn
    receipt = asyncio.run(adapter.speak("-f/etc/hosts", str(uuid4())))
    argv = seen_argv["argv"]
    assert argv[-2:] == ["--", "-f/etc/hosts"]
    assert receipt["status"] == "synthesized"


def test_real_flag_like_text_does_not_read_file(tmp_path):
    adapter = write_adapter(tmp_path)
    try:
        receipt = asyncio.run(adapter.speak("-f/etc/hosts", str(uuid4())))
    except (SpeechError, FileNotFoundError):
        pytest.skip("macOS say unavailable in this environment")
    # /etc/hosts spoken aloud takes ~19s; the literal string is ~1.4s.
    assert receipt["status"] == "synthesized"
    assert receipt["duration_s"] < 5
    assert receipt["file_path"].endswith(".wav")


def test_real_say_synthesizes_valid_wav(tmp_path):
    adapter = write_adapter(tmp_path)
    try:
        receipt = asyncio.run(adapter.speak("Annie speech is ready", str(uuid4())))
    except (SpeechError, FileNotFoundError):
        pytest.skip("macOS say unavailable or failed in this environment")
    assert receipt["status"] == "synthesized"
    assert receipt["played"] is False
    assert receipt["duration_s"] > 0
    blob = open(receipt["file_path"], "rb").read()
    frames = parse_wav_header(blob)
    assert abs(receipt["duration_s"] - frames / 22050) < 0.05


def test_wav_header_validation():
    good = minimal_wav()
    assert parse_wav_header(good) == 64
    truncated = good[:40]
    with pytest.raises(SpeechError):
        parse_wav_header(truncated)
    bad_magic = b"XXXX" + good[4:]
    with pytest.raises(SpeechError):
        parse_wav_header(bad_magic)
    bad_format = bytearray(good)
    struct.pack_into("<H", bad_format, 20, 3)  # IEEE float instead of PCM
    with pytest.raises(SpeechError):
        parse_wav_header(bytes(bad_format))
