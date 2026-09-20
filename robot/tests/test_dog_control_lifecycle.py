"""Manual-control lifecycle regressions: fake transport and mock audio only."""
import asyncio
import json
import threading

import pytest

from test_go2_patrol_greet_runtime import FakeDog, NobodyTracker, StraightBandit
from robot.dog.runtime import patrol
from robot.dog.sim_audio import MockAudio


def make_view():
    return patrol.LiveView(port=0)


async def start_runtime(monkeypatch, dog, view, audio, *, manual=True, duration=0.8, require_selection=False, demo_everyone=False):
    from robot.dog import inference
    monkeypatch.setattr(inference, "shared", lambda: None)
    original_sleep = asyncio.sleep

    async def quick_stand(delay):
        await original_sleep(0.005 if delay == 1.5 else delay)

    monkeypatch.setattr(asyncio, "sleep", quick_stand)
    task = asyncio.ensure_future(patrol.run_patrol_greet(
        ip="10.0.0.99", aes_key=None, conn_factory=lambda *_: dog, tracker=NobodyTracker(),
        encoder=lambda frame: (b"", 640, 480), status=lambda text: None,
        rate_hz=200, stale_s=0.2, lidar_stale_s=0.2, boundary_m=100,
        voxel_min_interval_s=0, bandit=StraightBandit(), frontier_planner=None,
        planner=patrol.PatrolPlanner(cruise_mps=0.25, turn_rps=0.5, backoff_s=0.03,
                                     min_turn_s=0.02, leash_m=50.0),
        stall=patrol.StallDetector(window_s=0.05, min_progress_m=0.02),
        view=view, duration_s=duration, idle_trick_s=0, source="simulation",
        audio=audio, faces_dir=".", manual_control=manual, require_grandma_selection=require_selection,
        demo_everyone_grandma=demo_everyone))
    while not hasattr(view, "missions"):
        assert not task.done(), f"runtime exited during setup: {getattr(task, 'exception', lambda: None)()}"
        await original_sleep(0.001)
    return task


def forward_moves(dog, start=0):
    return [json.loads(d["parameter"]) for t, d, _ in dog.sent[start:]
            if t == patrol.TOPIC_SPORT and json.loads(d["parameter"])["x"] > 0]


def test_manual_control_held_before_and_after_say(monkeypatch):
    dog, view, audio = FakeDog(step_m=0.001), make_view(), MockAudio(transcript="")

    async def run():
        task = await start_runtime(monkeypatch, dog, view, audio)
        while view.report["connection"]["status"] != "connected":
            assert not task.done()
            await asyncio.sleep(0.002)
        assert view.report["paused"], "manual start must hold before any mission"
        held_index = len(dog.sent)
        code, receipt = view.missions.submit({"command_id": "say-1", "name": "say",
                                              "args": {"text": "Holding position."}})
        assert code == 202
        while view.missions.get("say-1")["state"] != "completed":
            assert not task.done()
            await asyncio.sleep(0.002)
        report = await asyncio.wait_for(task, 5)
        assert report["paused"] and report["manual_control"]
        receipt = view.missions.get("say-1")
        assert receipt["result"]["played"] and receipt["result"]["where"] == "simulation mock"
        assert all(json.loads(d["parameter"])["x"] == json.loads(d["parameter"])["z"] == 0
                   for t, d, _ in dog.sent if t == patrol.TOPIC_SPORT), "no motion while held or speaking"

    asyncio.run(run())


def test_cancelled_planner_cannot_publish_stale_steps(monkeypatch):
    dog, view = FakeDog(step_m=0.001), make_view()
    gate_a, gate_b, started_b = threading.Event(), threading.Event(), threading.Event()
    original_plan = patrol.agent_mod.plan_instruction

    def slow_plan(text, sit, inference=None, validate_command=None):
        if text == "patrol for a bit":
            gate_a.wait(2)
        else:
            started_b.set()
            gate_b.wait(2)
        return original_plan(text, sit, inference=None, validate_command=validate_command)

    monkeypatch.setattr(patrol.agent_mod, "plan_instruction", slow_plan)

    async def run():
        task = await start_runtime(monkeypatch, dog, view, MockAudio(transcript=""))
        code, _ = view.missions.submit({"command_id": "instruct-a", "name": "instruct",
                                        "args": {"text": "patrol for a bit"}})
        assert code == 202
        while view.missions.get("instruct-a")["state"] != "executing":
            assert not task.done()
            await asyncio.sleep(0.002)
        view.missions.submit({"command_id": "stop-1", "name": "stop", "args": {}})
        while view.missions.get("instruct-a")["state"] != "cancelled":
            assert not task.done()
            await asyncio.sleep(0.002)
        view.missions.submit({"command_id": "instruct-b", "name": "instruct",
                              "args": {"text": "say hello nicely"}})
        while view.missions.get("instruct-b")["state"] not in ("executing", "completed", "failed"):
            assert not task.done()
            await asyncio.sleep(0.002)
        while not started_b.is_set():
            assert not task.done()
            await asyncio.sleep(0.002)
        gate_a.set()
        await asyncio.sleep(0.04)
        gate_b.set()
        report = await asyncio.wait_for(task, 5)
        assert report["reason"] == "duration_complete", report.get("error")
        a, b = view.missions.get("instruct-a"), view.missions.get("instruct-b")
        assert a["state"] == "cancelled"
        assert b["state"] == "completed", b
        results = (b.get("progress") or {}).get("results") or []
        assert results and all(r["name"] != "patrol" for r in results), \
            "stale planner output must not add patrol steps to the newer receipt"

    asyncio.run(run())


def test_timed_patrol_receipt_runs_to_deadline_then_stop_cancels(monkeypatch):
    dog, view, audio = FakeDog(step_m=0.001), make_view(), MockAudio(transcript="")

    async def run():
        task = await start_runtime(monkeypatch, dog, view, audio)
        code, _ = view.missions.submit({"command_id": "patrol-1", "name": "patrol",
                                        "args": {"duration_s": 5.0}})
        assert code == 202
        while view.missions.get("patrol-1")["state"] != "executing":
            assert not task.done()
            await asyncio.sleep(0.002)
        while not forward_moves(dog):
            assert not task.done() and view.missions.get("patrol-1")["state"] == "executing"
            await asyncio.sleep(0.002)
        assert view.missions.get("patrol-1")["state"] == "executing", \
            "a timed patrol receipt must stay executing while it explores"
        assert view.missions.get("patrol-1")["progress"]["step"] == "exploring"
        stop_index = len(dog.sent)
        view.missions.submit({"command_id": "stop-1", "name": "stop", "args": {}})
        while view.missions.get("patrol-1")["state"] != "cancelled":
            assert not task.done()
            await asyncio.sleep(0.002)
        report = await asyncio.wait_for(task, 5)
        assert report["paused"], "manual control must return to held after the stop"
        assert all(json.loads(d["parameter"])["x"] == 0 and json.loads(d["parameter"])["z"] == 0
                   for t, d, _ in dog.sent[stop_index:] if t == patrol.TOPIC_SPORT), \
            "after the stop the dog must hold neutral, not resume exploring"

    asyncio.run(run())


def test_say_failure_marks_receipt_failed(monkeypatch):
    dog, view = FakeDog(step_m=0.001), make_view()

    class Deaf:
        source = "simulation"

        def speak(self, text):
            return False

        def listen(self, max_s):
            return {"transcript": None, "heard": False, "speech_ms": 0}

    async def run():
        task = await start_runtime(monkeypatch, dog, view, Deaf())
        code, _ = view.missions.submit({"command_id": "say-2", "name": "say",
                                        "args": {"text": "Can anyone hear me?"}})
        assert code == 202
        while view.missions.get("say-2")["state"] not in ("failed", "completed"):
            assert not task.done()
            await asyncio.sleep(0.002)
        receipt = view.missions.get("say-2")
        assert receipt["state"] == "failed" and receipt["error"] == "speech playback failed"
        report = await asyncio.wait_for(task, 5)
        assert report["reason"] == "duration_complete"

    asyncio.run(run())


def test_planned_stop_child_uses_its_own_acknowledgment(monkeypatch):
    dog, view = FakeDog(), make_view()

    async def run():
        task = await start_runtime(monkeypatch, dog, view, MockAudio())
        # "halt" takes the normal planner path, unlike priority typed "stop".
        view.missions.submit({"command_id": "planned-stop", "name": "instruct", "args": {"text": "halt"}})
        await asyncio.wait_for(task, 5)
        receipt = view.missions.get("planned-stop")
        assert receipt["state"] == "completed", json.dumps(receipt)
        assert receipt["result"]["results"][0]["result"]["stop_code"] == 0
        assert view.report["paused"]

    asyncio.run(run())


def test_dialogue_records_mock_speech_and_reply_bounded_80(monkeypatch):
    dog, view, audio = FakeDog(step_m=0.001), make_view(), MockAudio()

    async def run():
        task = await start_runtime(monkeypatch, dog, view, audio)
        view.missions.submit({"command_id": "say-3", "name": "say", "args": {"text": "Hello there."}})
        while view.missions.get("say-3")["state"] != "completed":
            assert not task.done()
            await asyncio.sleep(0.002)
        view.missions.submit({"command_id": "hear-1", "name": "listen", "args": {"max_s": 1.0}})
        while view.missions.get("hear-1")["state"] not in ("completed", "failed"):
            assert not task.done()
            await asyncio.sleep(0.002)
        report = await asyncio.wait_for(task, 5)
        assert report["reason"] == "duration_complete"
        lines = patrol.telemetry_snapshot(view)["dialogue"]
        said = [l for l in lines if l["role"] == "annie" and "Hello there." in l["text"]]
        heard = [l for l in lines if l["role"] == "resident" and l["text"] == "I'm doing well, thank you."]
        assert said and said[0]["mocked"] and said[0]["source"] == "simulation"
        assert heard and heard[0]["mocked"]
        for i in range(90):
            view.record_dialogue("annie", f"filler {i}", source="simulation")
        assert len(patrol.telemetry_snapshot(view)["dialogue"]) == 80

    asyncio.run(run())
