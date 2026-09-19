"""Tests for native afplay playback. Subprocess is faked; never a real speaker."""
from __future__ import annotations

import io
import threading
import time
import wave

import pytest

from robot.simulation import native_audio
from robot.simulation.native_audio import (
    MAX_DURATION_S,
    NativeAudioError,
    NativeAudioPlayer,
)


def make_wav(tmp_path, name="clip.wav", seconds=0.2, rate=22050):
    frames = b"\x00\x00" * int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return path


class FakeProcess:
    def __init__(self, returncode=0, delay=0.0):
        self.returncode = returncode
        self.delay = delay
        self.killed = False

    def wait(self, timeout=None):
        if self.killed:
            return -9
        if self.delay > 0:
            time.sleep(min(self.delay, timeout or self.delay))
            if self.delay > (timeout or 0):
                raise native_audio.subprocess.TimeoutExpired("afplay", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


class FakePopenFactory:
    def __init__(self, returncode=0, delay=0.0, fail_spawn=False):
        self.returncode = returncode
        self.delay = delay
        self.fail_spawn = fail_spawn
        self.argv_seen = None

    def __call__(self, argv, **kwargs):
        self.argv_seen = argv
        if self.fail_spawn:
            raise OSError("binary gone")
        return FakeProcess(self.returncode, self.delay)


@pytest.fixture
def player(tmp_path):
    return NativeAudioPlayer(output_dir=tmp_path)


def test_play_success_returns_process_completed_status(tmp_path, player):
    wav = make_wav(tmp_path)
    player.afplay_path = "afplay"
    fake = FakePopenFactory(returncode=0)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = fake
    try:
        result = player.play(wav, duration_s=0.2)
    finally:
        native_audio.subprocess.Popen = orig
    assert result["status"] == "playback_process_completed"
    assert result["ended_monotonic"] >= result["started_monotonic"]
    assert result["ended_wall"] >= result["started_wall"]
    assert fake.argv_seen == ["afplay", str(wav.resolve())]
    assert "shell" not in str(fake.argv_seen)


def test_play_nonzero_exit_raises_domain_error(tmp_path, player):
    wav = make_wav(tmp_path)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(returncode=1)
    try:
        with pytest.raises(NativeAudioError, match="status 1"):
            player.play(wav, duration_s=0.2)
    finally:
        native_audio.subprocess.Popen = orig


def test_play_missing_binary_raises_domain_error(tmp_path, player):
    wav = make_wav(tmp_path)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(fail_spawn=True)
    try:
        with pytest.raises(NativeAudioError, match="Cannot start"):
            player.play(wav, duration_s=0.2)
    finally:
        native_audio.subprocess.Popen = orig


def test_play_timeout_kills_process(monkeypatch, tmp_path, player):
    wav = make_wav(tmp_path)
    proc = FakeProcess(returncode=0, delay=10.0)

    def fake_popen(argv, **kwargs):
        return proc

    monkeypatch.setattr(native_audio.subprocess, "Popen", fake_popen)
    with pytest.raises(NativeAudioError, match="timed out"):
        player.play(wav, duration_s=0.2)
    assert proc.killed


def test_timeout_is_bounded_by_min_duration_plus_five_35(monkeypatch, tmp_path):
    captured = {}

    class WaitCapture(FakeProcess):
        def wait(self, timeout=None):
            captured["timeout"] = timeout
            return 0

    monkeypatch.setattr(native_audio.subprocess, "Popen",
                        lambda argv, **kw: WaitCapture())
    p = NativeAudioPlayer(output_dir=tmp_path)
    p.play(make_wav(tmp_path), duration_s=0.2)
    assert captured["timeout"] == pytest.approx(5.2)
    # Max valid duration clamps the hard timeout at 35 s.
    p.play(make_wav(tmp_path), duration_s=30.0)
    assert captured["timeout"] == pytest.approx(35.0)


def test_rejects_path_outside_output_dir(tmp_path, player):
    outside = make_wav(tmp_path.parent, name="outside.wav")
    with pytest.raises(NativeAudioError, match="output directory"):
        player.play(outside, duration_s=0.2)


def test_rejects_non_wav_suffix(tmp_path, player):
    txt = tmp_path / "clip.txt"
    txt.write_bytes(b"nope")
    with pytest.raises(NativeAudioError, match=r"\.wav"):
        player.play(txt, duration_s=0.2)


def test_rejects_missing_file(tmp_path, player):
    with pytest.raises(NativeAudioError, match="does not exist"):
        player.play(tmp_path / "ghost.wav", duration_s=0.2)


def test_rejects_oversized_file(tmp_path, player, monkeypatch):
    wav = make_wav(tmp_path)
    player.max_file_bytes = 100  # smallest possible bound; clip is larger
    with pytest.raises(NativeAudioError, match="exceeds"):
        player.play(wav, duration_s=0.2)


def test_rejects_bad_durations(tmp_path, player):
    wav = make_wav(tmp_path)
    for bad in (0, -1, 31.0, float("nan"), "0.2", True):
        with pytest.raises(NativeAudioError):
            player.play(wav, duration_s=bad)


def test_enqueue_rejects_when_full(tmp_path):
    p = NativeAudioPlayer(output_dir=tmp_path, queue_size=2)
    wav = str(make_wav(tmp_path))
    p.enqueue({"file_path": wav, "duration_s": 0.2})
    p.enqueue({"file_path": wav, "duration_s": 0.2})
    with pytest.raises(NativeAudioError, match="queue is full"):
        p.enqueue({"file_path": wav, "duration_s": 0.2})


def test_enqueue_then_drain_emits_playing_played(tmp_path):
    p = NativeAudioPlayer(output_dir=tmp_path, queue_size=2)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(returncode=0, delay=0.05)
    states = []
    done = threading.Event()

    def cb(payload):
        states.append(payload["state"])
        if payload["state"] == "played":
            done.set()

    p.start()
    p.enqueue({"file_path": str(make_wav(tmp_path)), "duration_s": 0.2},
              on_state_callback=cb)
    assert done.wait(timeout=5.0)
    assert states[:2] == ["playing", "played"]
    p.stop()
    native_audio.subprocess.Popen = orig


def test_worker_continues_after_failure(tmp_path):
    p = NativeAudioPlayer(output_dir=tmp_path, queue_size=4)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(returncode=3)
    states = []
    failed = threading.Event()
    played = threading.Event()

    def cb(payload):
        states.append(payload["state"])
        if payload["state"] == "failed":
            failed.set()
            # Next clip succeeds: swap factory before first wait returns is
            # racy; instead use a second player run to confirm recovery.

    def cb2(payload):
        states.append(payload["state"])
        if payload["state"] == "played":
            played.set()

    p.start()
    p.enqueue({"file_path": str(make_wav(tmp_path)), "duration_s": 0.2},
              on_state_callback=cb)
    assert failed.wait(timeout=5.0)
    native_audio.subprocess.Popen = FakePopenFactory(returncode=0)
    p.enqueue({"file_path": str(make_wav(tmp_path)), "duration_s": 0.2},
              on_state_callback=cb2)
    assert played.wait(timeout=5.0)
    assert states == ["playing", "failed", "playing", "played"]
    p.stop()
    native_audio.subprocess.Popen = orig


def test_callback_exception_does_not_kill_worker(tmp_path):
    p = NativeAudioPlayer(output_dir=tmp_path, queue_size=2)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(returncode=0, delay=0.02)
    done = threading.Event()

    def bad_cb(payload):
        raise RuntimeError("callback bug")

    def good_cb(payload):
        if payload["state"] == "played":
            done.set()

    p.start()
    p.enqueue({"file_path": str(make_wav(tmp_path)), "duration_s": 0.2},
              on_state_callback=bad_cb)
    p.enqueue({"file_path": str(make_wav(tmp_path)), "duration_s": 0.2},
              on_state_callback=good_cb)
    assert done.wait(timeout=5.0)
    p.stop()
    native_audio.subprocess.Popen = orig


def test_status_never_claims_human_heard(tmp_path, player):
    wav = make_wav(tmp_path)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(returncode=0)
    try:
        result = player.play(wav, duration_s=0.2)
    finally:
        native_audio.subprocess.Popen = orig
    text = repr(result)
    assert "playback_process_completed" in text
    assert "heard" not in text.lower()


def test_relative_path_resolves_under_output_dir(tmp_path, player):
    wav = make_wav(tmp_path)
    orig = native_audio.subprocess.Popen
    native_audio.subprocess.Popen = FakePopenFactory(returncode=0)
    try:
        result = player.play(wav.name, duration_s=0.2)
    finally:
        native_audio.subprocess.Popen = orig
    assert result["status"] == "playback_process_completed"
