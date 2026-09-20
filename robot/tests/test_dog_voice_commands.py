"""Command gating regressions; all microphone, clock and STT inputs are fake."""
import sys
from types import SimpleNamespace

import pytest

from robot.dog.voice import commands


@pytest.mark.parametrize("phrase", [
    "Oh it means stop recording. By all means", "please wave to the camera",
    "we should sit down", "Okay stop",
])
def test_unwoken_conversation_is_not_a_physical_command(phrase):
    assert commands.parse_command(phrase, require_wake=False)["intent"] == "instruct"
    assert commands.parse_command(phrase) is None


@pytest.mark.parametrize("phrase", ["Stop!", "pause", "please stop", "stop please"])
def test_precise_stop_without_wake_in_conversation(phrase):
    assert commands.parse_command(phrase, require_wake=False)["intent"] == "stop"


def test_addressed_wave_remains_command():
    assert commands.parse_command("Annie, wave")["intent"] == "hello"


def run_listener(monkeypatch, *, timing=None, phrase="Annie wave"):
    now = [100.0]
    calls, transcriptions = [], []
    monkeypatch.setattr(commands.time, "time", lambda: now[0])
    listener = commands.CommandListener(lambda cmd, text: calls.append(cmd),
                                        status=lambda *_: None, recorder=object(), vad=object())
    listener.open_conversation()
    if timing == "start":
        listener.mute(1)

    def capture(*args, **kwargs):
        listener.stop()  # one utterance, executed synchronously, no background thread
        if timing == "capture":
            listener.mute(1)
        if timing in ("capture", "start"):
            now[0] += 2  # playback ends before capture returns
        return {"outcome": "speech", "pcm": b"audio"}

    def transcribe(wav):
        transcriptions.append(wav)
        if timing == "transcription":
            listener.mute(1)
            now[0] += 2  # playback and mute expire before STT returns
        return phrase

    listener._transcriber = transcribe
    monkeypatch.setitem(sys.modules, "robot.simulation.live_listener", SimpleNamespace(
        capture_utterance=capture, pcm16_to_wav=lambda pcm: pcm,
        silero_vad=lambda: pytest.fail("real VAD requested"),
        sounddevice_recorder=lambda: pytest.fail("real microphone requested")))
    monkeypatch.setitem(sys.modules, "robot.simulation.local_stt", SimpleNamespace(
        LocalSTTAdapter=lambda: pytest.fail("real STT requested")))
    listener._loop()
    return calls, transcriptions


@pytest.mark.parametrize("timing", ["start", "capture", "transcription"])
def test_expired_mute_still_discards_contaminated_utterance(monkeypatch, timing):
    calls, transcriptions = run_listener(monkeypatch, timing=timing)
    assert calls == []
    assert len(transcriptions) == (1 if timing == "transcription" else 0)


@pytest.mark.parametrize("phrase,intent", [
    ("Oh it means stop recording. By all means", "converse"),
    ("Annie wave", "hello"), ("stop", "stop"), ("please pause", "stop"),
])
def test_listener_conversation_routes(monkeypatch, phrase, intent):
    calls, _ = run_listener(monkeypatch, phrase=phrase)
    assert [cmd["intent"] for cmd in calls] == [intent]
