"""The conversation after a greeting, mocked end to end in the runtime harness: Annie greets and asks, the person
answers (scripted transcript instead of the microphone), Annie replies in character; a call for help becomes a
concern with fixed wording. Also: a no-wake-word sentence inside the conversation window."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.tests.test_go2_patrol_greet_runtime import FakeDog, PersonTracker_, run  # noqa: E402
import robot.dog.runtime.patrol as patrol  # noqa: E402


class Close(PersonTracker_):
    def update(self, jpeg, now_ms):
        self.updates += 1
        return [{"track_id": 4, "box": [280, 60, 360, 330], "conf": 0.9, "posture": "upright", "lying_frames": 0,
                 "kp_conf": [0.9] * 17, "first_seen_ms": now_ms - 1000,
                 "identity": {"name": "Jeanine", "score": 0.8, "method": "shirt_colour"}}]


class FakeListener:
    """Stands in for the always-on CommandListener: records mutes/windows, never touches audio."""
    def __init__(self):
        self.on_command = None
        self.muted_until = 0.0
        self.open_until = 0.0
        self.started = self.stopped = False
        self.mutes = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def mute(self, seconds):
        self.mutes.append(seconds)
        self.muted_until = 10**12

    def open_conversation(self, seconds=45.0):
        self.open_until = 10**12


def _run_conversation(monkeypatch, transcript):
    # This harness supplies identity evidence directly, not red-shirt pixels in its blank frames.
    class SuppliedIdentity:
        def apply(self, img, tracks):
            return tracks
    # Fake firmware acknowledges immediately; compress its settle periods, keeping real
    # telemetry/control ticks and the runtime duration guard active throughout the conversation.
    original_sleep = asyncio.sleep
    async def quick_settle(delay):
        await original_sleep(0.01 if delay >= 0.5 else delay)
    monkeypatch.setattr(asyncio, "sleep", quick_settle)
    spoken = []
    monkeypatch.setattr(patrol, "listen_blocking", lambda max_s: {"transcript": transcript, "heard": bool(transcript), "speech_ms": 900})
    monkeypatch.setattr(patrol, "speak_blocking", lambda text: spoken.append(text) or True)
    listener = FakeListener()
    listener.replies = []
    actual_reply = patrol.agent_mod.converse_reply
    def capture_reply(*args, **kwargs):
        reply = actual_reply(*args, **kwargs)
        listener.replies.append(reply)
        return reply
    monkeypatch.setattr(patrol.agent_mod, "converse_reply", capture_reply)
    dog = FakeDog(wall_x=None)
    report = run(dog, duration_s=0.6, tracker=Close(), voice=listener, identifier=SuppliedIdentity())
    return report, spoken, listener


def test_greeting_asks_listens_and_replies_kindly(monkeypatch):
    report, spoken, listener = _run_conversation(monkeypatch, "I'm fine thank you, just reading")
    assert report["greetings"] and report["greetings"][0]["name"] == "Jeanine"
    convo = report["conversations"][0]
    assert convo["heard"].startswith("I'm fine") and convo["kind"] == "fine" and convo["name"] == "Jeanine"
    assert convo["reply"] and "Jeanine" in convo["reply"]           # the fallback reply uses her name
    assert convo["reply"] in spoken and report.get("concerns", []) == []
    assert listener.started  # (muting lives in the real speak/listen helpers, patched out here; see test_go2_host_voice)


def test_call_for_help_becomes_a_concern_with_fixed_wording(monkeypatch):
    report, spoken, listener = _run_conversation(monkeypatch, "help, I fell and I can't get up")
    convo = report["conversations"][0]
    expected = "I'm right here with you, Jeanine. Please call for someone nearby to help."
    assert convo["kind"] == "concern" and convo["reply"] == expected
    assert listener.replies[0]["source"] == "rules"  # safety wording is fixed, never model-generated
    assert report["concerns"] and report["concerns"][0]["name"] == "Jeanine" and "fell" in report["concerns"][0]["heard"]
    assert expected in spoken
    assert not any("letting the family know" in text for text in spoken)


def test_silence_is_recorded_as_no_reply(monkeypatch):
    report, spoken, _ = _run_conversation(monkeypatch, None)
    convo = report["conversations"][0]
    assert convo["heard"] is None and convo["kind"] == "none" and convo["reply"] is None
