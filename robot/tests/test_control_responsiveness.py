"""Control diagnostics and stop responsiveness; fake transport/audio only."""
import asyncio
import json
import threading
import time

import pytest

from robot.dog.perception.pipeline import Diag
from robot.dog.runtime import patrol
from test_go2_patrol_greet_runtime import FakeDog, NobodyTracker, StraightBandit


def test_diagnostics_distinguish_behaviour_safety_and_stalled_checks(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("robot.dog.perception.pipeline.time.monotonic", lambda: clock[0])
    diag = Diag(window_s=1)
    assert diag.snapshot()["safety_age_ms"] is None
    diag.tick("control")
    for i in range(20):
        diag.tick("safety", now=10 + i / 20)
    clock[0] = 11.0
    snapshot = diag.snapshot()
    assert snapshot["control_hz"] == 1
    assert snapshot["safety_hz"] == 20
    assert snapshot["motion_tx_hz"] == 0  # checks do not invent motor commands
    assert snapshot["safety_max_gap_ms"] == 50
    clock[0] = 13.0
    snapshot = diag.snapshot()
    assert snapshot["safety_hz"] == 0
    assert snapshot["safety_age_ms"] == snapshot["safety_max_gap_ms"] == 2050


class SlowAudio:
    source = "simulation"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def speak(self, text):
        if text == "slow test speech":
            self.entered.set()
            self.release.wait(3)
        return True

    def listen(self, max_s):
        return {"transcript": None, "heard": False, "source": "simulation"}


class FakeVoice:
    on_command = None

    def start(self):
        pass

    def stop(self):
        pass


@pytest.mark.parametrize("stop_source", ["app", "voice"])
def test_stop_is_serviced_while_audio_is_pending(monkeypatch, stop_source):
    # Compress setup waits only, preserving real control/guard timing.
    original_sleep = asyncio.sleep

    async def quick_setup(delay):
        await original_sleep(0.01 if delay >= 1 else delay)

    monkeypatch.setattr(asyncio, "sleep", quick_setup)
    monkeypatch.setattr(patrol, "LISTENER", None)
    dog, audio, voice = FakeDog(step_m=0.001), SlowAudio(), FakeVoice()
    view = patrol.LiveView(port=0)

    async def exercise():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="fake", aes_key=None, conn_factory=lambda *_: dog,
            tracker=NobodyTracker(), encoder=lambda frame: (b"", 640, 480),
            audio=audio, voice=voice, source="simulation", status=lambda text: None,
            view=view, duration_s=4, boundary_m=100, stale_s=0.5,
            lidar_stale_s=0.5, bandit=StraightBandit(), frontier_planner=None,
            idle_trick_s=0, diag_every_s=10,
        ))
        try:
            deadline = time.monotonic() + 2
            while not hasattr(view, "missions"):
                assert not task.done(), task.result() if task.done() else None
                assert time.monotonic() < deadline
                await original_sleep(0.005)
            code, _ = view.missions.submit({"command_id": "slow-speech", "name": "say",
                                           "args": {"text": "slow test speech"}})
            assert code == 202
            while not audio.entered.is_set():
                assert not task.done(), task.result() if task.done() else None
                assert time.monotonic() < deadline
                await original_sleep(0.005)
            await original_sleep(0.65)
            # The outer loop remains inside say; diagnostics must still be fresh.
            diag = dict(view.state.get("diag") or {})
            assert diag.get("safety_hz", 0) > 0, diag
            assert diag["safety_hz"] > diag["control_hz"]
            assert diag["safety_age_ms"] < 150
            assert diag["safety_max_gap_ms"] < 150
            assert not audio.release.is_set()
            sent_before_stop = len(dog.sent)
            requests_before_stop = len(dog.requests)
            started = time.monotonic()
            if stop_source == "voice":
                voice.on_command({"intent": "stop"}, "stop")
            else:
                view.missions.submit({"command_id": "stop", "name": "stop", "args": {}})
            while not any(options["api_id"] == patrol.STOP_MOVE
                          for _, options in dog.requests[requests_before_stop:]):
                assert time.monotonic() - started < 0.3
                await original_sleep(0.005)
            elapsed = time.monotonic() - started
            assert elapsed < 0.3
            assert view.missions.get("slow-speech")["state"] in ("cancelled", "failed")
            # A late provider completion must not finish the cancelled mission or resume motion.
            audio.release.set()
            await original_sleep(0.15)
            assert view.missions.get("slow-speech")["state"] in ("cancelled", "failed")
            later = [json.loads(data["parameter"]) for topic, data, _ in dog.sent[sent_before_stop:]
                     if topic == patrol.TOPIC_SPORT]
            assert later and all(p["x"] == p["z"] == 0 for p in later)
            print(f"{stop_source} StopMove request during pending audio: {elapsed * 1000:.1f} ms; "
                  f"guard gap {diag['safety_max_gap_ms']:.1f} ms")
        finally:
            audio.release.set()
            if not task.done():
                view.missions.submit({"name": "stop", "args": {}})
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
