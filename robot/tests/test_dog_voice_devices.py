"""Speaker/microphone selection by name, and the runtime voice switch (cloud vs local)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.dog.voice.cloud import CloudVoice  # noqa: E402
from robot.dog.voice.devices import AudioDevices, resolve  # noqa: E402

DEVS = [{"index": 0, "name": "Henry’s iPhone Microphone", "input": True, "output": False},
        {"index": 1, "name": "AirPods Pro", "input": True, "output": False},
        {"index": 2, "name": "AirPods Pro", "input": False, "output": True},
        {"index": 3, "name": "MacBook Pro Microphone", "input": True, "output": False},
        {"index": 4, "name": "MacBook Pro Speakers", "input": False, "output": True}]


def test_resolve_by_name_kind_and_substring():
    assert resolve("AirPods Pro", kind="input", devices=DEVS) == 1
    assert resolve("airpods", kind="output", devices=DEVS) == 2
    assert resolve("iphone", kind="input", devices=DEVS) == 0
    assert resolve("iphone", kind="output", devices=DEVS) is None      # no such speaker
    assert resolve(None, kind="input", devices=DEVS) is None and resolve("  ", kind="output", devices=DEVS) is None


def test_configure_bumps_version_and_clears_with_empty():
    d = AudioDevices(input_name="AirPods Pro", output_name=None)
    assert d.version == 0
    d.configure(output_name="MacBook Pro Speakers")
    assert (d.input_name, d.output_name, d.version) == ("AirPods Pro", "MacBook Pro Speakers", 1)
    d.configure(input_name="")
    assert d.input_name is None and d.version == 2


def test_cloud_voice_runtime_switch_and_keys_never_in_status():
    calls = []
    v = CloudVoice(eleven_key="e-key", deepgram_key="d-key", local_speak=lambda t: calls.append(("local", t)) or True,
                   local_transcribe=lambda w: "local words", tts=lambda text, api_key, voice_id: b"mp3", stt=lambda wav, api_key: "cloud words",
                   player=lambda audio: calls.append(("cloud", audio)) or True)
    assert v.status()["speak_via"] == "elevenlabs" and v.status()["hear_via"] == "deepgram"
    assert v.speak("hi") and calls[-1][0] == "cloud" and v.transcribe(b"wav") == "cloud words"
    st = v.configure(cloud=False)
    assert st["speak_via"] == "local" and st["hear_via"] == "local" and "e-key" not in str(st) and "d-key" not in str(st)
    assert v.speak("hi") and calls[-1][0] == "local" and v.transcribe(b"wav") == "local words"
    st = v.configure(cloud=True, deepgram_key="")   # key cleared: hearing falls back to local, speaking stays cloud
    assert st["hear_via"] == "local" and st["speak_via"] == "elevenlabs"
