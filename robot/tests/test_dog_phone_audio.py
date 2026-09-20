"""The "Annie Audio" phone app as a microphone/speaker: a real in-process WebSocket client against the server on
an ephemeral loopback port. No phone, no audio hardware, no network beyond 127.0.0.1."""
import io
import shutil
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

np = pytest.importorskip("numpy")
pytest.importorskip("websockets")
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from robot.dog.voice import devices  # noqa: E402
from robot.dog.voice import phone_audio  # noqa: E402
from robot.dog.voice.devices import PHONE_DEVICE, PHONE_INDEX, AudioDevices, list_devices, resolve  # noqa: E402
from robot.dog.voice.phone_audio import PhoneAudioServer, decode_to_pcm16  # noqa: E402

PACKET = 320  # the phone's 20 ms packets


def _until(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _client(server, path="/audio"):
    return connect(f"ws://127.0.0.1:{server.status()['port']}{path}", compression=None)


def _voiced(recorder, samples, timeout_s=3.0):
    """Concatenate the non-silent chunks until `samples` samples arrived (silence ticks may come first)."""
    got, deadline = [], time.monotonic() + timeout_s
    while sum(c.size for c in got) < samples and time.monotonic() < deadline:
        chunk = next(recorder)
        assert chunk.shape == (phone_audio.CHUNK_SAMPLES,) and chunk.dtype == np.int16
        if chunk.any():
            got.append(chunk)
    return np.concatenate(got) if got else np.zeros(0, dtype=np.int16)


@pytest.fixture
def server():
    decoded = []
    srv = PhoneAudioServer(host="127.0.0.1", port=0, decoder=lambda data, suffix, timeout_s: decoded.append(suffix) or data)
    srv.decoded = decoded
    srv.start()
    assert srv.status()["listening"] and srv.status()["port"] > 0 and srv.status()["error"] is None
    yield srv
    srv.stop()
    assert not srv.status()["listening"]


def test_chunking_matches_the_wake_word_listener():
    from robot.simulation import live_listener
    assert (phone_audio.RATE, phone_audio.CHUNK_SAMPLES) == (live_listener.RATE, live_listener.CHUNK_SAMPLES)


def test_phone_packets_come_out_of_the_recorder_rechunked(server):
    recorder = server.recorder()
    ramp = np.arange(1, 8 * PACKET + 1, dtype=np.int16)  # 2560 samples = exactly five 512-sample chunks
    with _client(server) as phone:
        assert _until(lambda: server.status()["connected"])
        assert server.status()["peer"] == "127.0.0.1"
        phone.send("text frames are ignored")
        for i in range(8):
            phone.send(ramp[i * PACKET:(i + 1) * PACKET].tobytes())
        heard = _voiced(recorder, ramp.size)
        assert np.array_equal(heard, ramp)
        assert server.status()["rx_packets"] == 8
    assert _until(lambda: not server.status()["connected"]) and server.status()["peer"] is None
    recorder.close()
    assert server._taps == []


def test_every_open_recorder_hears_the_whole_stream_and_queues_are_bounded(server):
    first, second = server.recorder(), server.recorder()
    tone = np.full(1024, 7, dtype=np.int16)
    with _client(server) as phone:
        phone.send(tone.tobytes())
        assert np.array_equal(_voiced(first, 1024), tone) and np.array_equal(_voiced(second, 1024), tone)
        for _ in range(6):  # 3 s nobody reads: only about 2 s may stay queued, the newest
            phone.send(np.full(8000, 9, dtype=np.int16).tobytes())
        phone.send(np.full(512, 11, dtype=np.int16).tobytes())
        assert _until(lambda: server.status()["rx_packets"] == 8)
        assert server._taps[0].samples <= int(phone_audio.MAX_QUEUED_S * phone_audio.RATE)
        assert server._taps[0].chunks[-1][0] == 11


def test_no_phone_recorder_yields_silence_and_play_is_false(server):
    recorder = server.recorder()
    started = time.monotonic()
    chunks = [next(recorder) for _ in range(5)]
    assert time.monotonic() - started < 1.0  # ticks at about the pace of a real microphone, never blocks for long
    assert all(c.shape == (phone_audio.CHUNK_SAMPLES,) and c.dtype == np.int16 and not c.any() for c in chunks)
    assert server.play(b"mp3 bytes") is False and server.send(b"\x00\x00") is False
    assert server.decoded == []  # nothing is decoded (no temp file) when nobody can hear it
    assert server.status()["connected"] is False and server.status()["tx_bytes"] == 0


def test_play_sends_the_decoded_pcm_in_small_even_paced_frames(server):
    pcm = (np.arange(9600, dtype=np.int16) - 4800).tobytes() + b"\x7f"  # 0.6 s and a stray odd byte
    frames = []
    with _client(server) as phone:
        assert _until(lambda: server.status()["connected"])

        def receive():
            while sum(map(len, frames)) < len(pcm) - 1:
                frames.append(phone.recv(timeout=3))

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        started = time.monotonic()
        assert server.play(pcm, suffix=".mp3") is True
        elapsed = time.monotonic() - started
        reader.join(3)
    assert b"".join(frames) == pcm[:-1] and server.decoded == [".mp3"]
    assert len(frames) > 1 and all(len(f) <= 8192 and len(f) % 2 == 0 for f in frames)
    assert 0.6 <= elapsed < 2.0  # blocks for the audio's duration so the dog does not listen over its own voice
    assert server.status()["tx_bytes"] == len(pcm) - 1


def test_newest_phone_wins_and_other_paths_are_refused(server):
    recorder = server.recorder()
    with pytest.raises(InvalidStatus):
        _client(server, path="/elsewhere").close()
    with _client(server) as old, _client(server) as new:
        with pytest.raises(ConnectionClosed):
            old.recv(timeout=3)
        assert server.status()["connected"]
        new.send(np.full(512, 5, dtype=np.int16).tobytes())
        assert np.array_equal(_voiced(recorder, 512), np.full(512, 5, dtype=np.int16))
        assert server.send(b"\x01\x00\x02\x00") is True and new.recv(timeout=3) == b"\x01\x00\x02\x00"


@pytest.mark.skipif(shutil.which("afconvert") is None, reason="afconvert is macOS only")
def test_afconvert_decodes_to_16k_mono_and_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    t = np.arange(int(0.25 * 22050)) / 22050.0
    stereo = np.repeat((8000 * np.sin(2 * np.pi * 440 * t)).astype("<i2")[:, None], 2, axis=1)
    source = io.BytesIO()
    with wave.open(source, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(22050)
        w.writeframes(stereo.tobytes())
    pcm = decode_to_pcm16(source.getvalue(), ".wav")
    assert pcm and abs(len(pcm) / 2 - 0.25 * 16000) <= 64 and np.abs(np.frombuffer(pcm, dtype="<i2")).max() > 4000
    assert decode_to_pcm16(b"not audio", ".mp3") is None
    assert list(tmp_path.iterdir()) == []


def test_devices_route_the_virtual_phone_by_name(server, monkeypatch):
    monkeypatch.setattr(devices, "_PHONE", None)
    assert resolve(PHONE_DEVICE, kind="input") == PHONE_INDEX and resolve("iphone (annie audio)", kind="output") == PHONE_INDEX
    assert resolve("annie audio", kind="output", devices=[]) == PHONE_INDEX
    assert all(d["name"] != PHONE_DEVICE for d in list_devices())  # not offered until its server exists
    monkeypatch.setattr(devices, "_PHONE", server)
    entry = [d for d in list_devices() if d["name"] == PHONE_DEVICE]
    assert entry == [{"index": PHONE_INDEX, "name": PHONE_DEVICE, "input": True, "output": True, "virtual": True}]
    chosen = AudioDevices(input_name=PHONE_DEVICE, output_name=PHONE_DEVICE)
    recorder = chosen.recorder()
    assert chosen.play(b"mp3") is False  # no phone yet: CloudVoice falls back to the local voice
    with _client(server) as phone:
        assert _until(lambda: chosen.status()["phone"]["connected"])
        phone.send(np.full(512, 3, dtype=np.int16).tobytes())
        assert np.array_equal(_voiced(recorder, 512), np.full(512, 3, dtype=np.int16))
        assert chosen.play(b"\x05\x00" * 800) is True and phone.recv(timeout=3) == b"\x05\x00" * 800
    status = chosen.status()
    assert (status["input"], status["input_index"], status["output_index"]) == (PHONE_DEVICE, PHONE_INDEX, PHONE_INDEX)
    assert status["phone"]["port"] == server.status()["port"] and status["phone"]["tx_bytes"] == 1600


def test_selecting_the_phone_starts_its_server_lazily(monkeypatch):
    monkeypatch.setattr(devices, "_PHONE", None)
    monkeypatch.setenv("ANNIE_PHONE_AUDIO_PORT", "0")
    monkeypatch.setenv("ANNIE_PHONE_AUDIO_HOST", "127.0.0.1")
    monkeypatch.delenv("ANNIE_AUDIO_INPUT", raising=False)
    monkeypatch.delenv("ANNIE_AUDIO_OUTPUT", raising=False)
    chosen = AudioDevices()
    assert chosen.status()["phone"]["listening"] is False and devices.phone_server() is None
    try:
        status = chosen.configure(output_name=PHONE_DEVICE)
        assert status["phone"]["listening"] is True and status["phone"]["port"] > 0 and status["output_index"] == PHONE_INDEX
        assert any(d["name"] == PHONE_DEVICE for d in status["devices"])
        assert status["input"] == "system default"  # the microphone choice is independent of the speaker
    finally:
        if devices.phone_server() is not None:
            devices.phone_server().stop()
