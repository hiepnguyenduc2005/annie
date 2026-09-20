"""Explicit exploration keeps social behavior; other missions and idle hold do not."""
import asyncio
import json

import pytest

from test_go2_patrol_greet_runtime import FakeDog, StraightBandit
from robot.dog.runtime import patrol
from robot.dog.sim_audio import MockAudio


class ClosePerson:
    def update(self, jpeg, now_ms):
        return [{"track_id": 4, "box": [280, 60, 360, 330], "conf": 0.9,
                 "posture": "upright", "lying_frames": 0, "kp_conf": [0.9] * 17,
                 "first_seen_ms": now_ms - 1000}]


def run_social_case(monkeypatch, tmp_path, command, *, autonomous=False, tracker=None, dog=None, duration=3.0, after_submit=None, **runtime_options):
    from robot.dog import inference
    monkeypatch.setattr(inference, "shared", lambda: None)
    original_sleep = asyncio.sleep

    async def quick_settle(delay):
        await original_sleep(0.005 if delay >= 0.8 else delay)

    monkeypatch.setattr(asyncio, "sleep", quick_settle)
    dog, view = dog or FakeDog(step_m=0.001), patrol.LiveView(port=0)
    audio = MockAudio(transcript="")

    async def run():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="unused", aes_key=None, conn_factory=lambda *_: dog,
            tracker=tracker or ClosePerson(), encoder=lambda frame: (b"", 640, 480),
            status=lambda text: None, rate_hz=200, stale_s=0.2, lidar_stale_s=0.2,
            boundary_m=100, voxel_min_interval_s=0, bandit=StraightBandit(),
            frontier_planner=None, view=view, duration_s=duration, idle_trick_s=0,
            source="simulation", audio=audio, faces_dir=str(tmp_path / "faces"),
            manual_control=not autonomous, demo_everyone_grandma=autonomous, autonomous_demo=autonomous, **runtime_options))
        while not hasattr(view, "missions"):
            assert not task.done()
            await original_sleep(0.001)
        while not view.state.get("tracks"):
            assert not task.done()
            await original_sleep(0.005)
        await original_sleep(0.03)
        if not autonomous:
            assert view.report["paused"]
            assert not hello_requests(dog)
        if command is not None:
            code, _ = view.missions.submit(dict(command, command_id="social-test"))
            assert code == 202
        if after_submit is not None:
            await after_submit(task, view, dog, original_sleep)
        report = await asyncio.wait_for(task, duration + 4)
        assert report["reason"] == "duration_complete", report.get("error")
        assert dog.disconnected
        return report

    return asyncio.run(run()), dog, view


def hello_requests(dog):
    return [options for topic, options in dog.requests
            if topic == patrol.TOPIC_SPORT and options["api_id"] == patrol.HELLO]


def test_active_patrol_greets_close_person(monkeypatch, tmp_path):
    report, dog, view = run_social_case(monkeypatch, tmp_path, {"name": "patrol", "args": {"duration_s": 5}})
    assert len(hello_requests(dog)) == 1, (report, view.missions.get("social-test"), dog.requests)
    assert report["greetings"] and report["greetings"][0]["track_id"] == 4
    assert view.missions.get("social-test")["progress"]["step"] == "exploring"


@pytest.mark.parametrize("command", [
    {"name": "go_home", "args": {}},
    {"name": "find_person", "args": {"name": "Jeanine", "timeout_s": 5, "approach": True}},
])
def test_other_missions_do_not_greet(monkeypatch, tmp_path, command):
    report, dog, view = run_social_case(monkeypatch, tmp_path, command)
    assert not hello_requests(dog)
    assert not report["greetings"]
    receipt = view.missions.get("social-test")
    assert receipt["state"] != "accepted", receipt


def test_manual_idle_holds_despite_close_person(monkeypatch, tmp_path):
    report, dog, _ = run_social_case(monkeypatch, tmp_path, None)
    assert report["paused"]
    assert not hello_requests(dog) and not report["greetings"]
    moves = [json.loads(data["parameter"]) for topic, data, _ in dog.sent
             if topic == patrol.TOPIC_SPORT]
    assert moves and all(move["x"] == move["z"] == 0 for move in moves)


def test_autonomous_demo_greets_without_command(monkeypatch, tmp_path):
    report, dog, _ = run_social_case(monkeypatch, tmp_path, None, autonomous=True)
    assert len(hello_requests(dog)) == 1
    assert report["greetings"] and report["greetings"][0]["track_id"] == 4
    assert not report["manual_control"]


class MovingPerson(ClosePerson):
    box = [280, 60, 360, 330]

    def update(self, jpeg, now_ms):
        tracks = super().update(jpeg, now_ms)
        tracks[0]["box"] = list(self.box)
        return tracks


def test_errand_arrival_holds_between_say_and_listen(monkeypatch, tmp_path):
    tracker = MovingPerson()

    async def check_hold(task, view, dog, sleep):
        while view.missions.get("social-test")["state"] != "completed":
            assert not task.done()
            await sleep(0.005)
        assert view.missions.get("social-test")["result"]["approached"]
        # Keep the person visible but move them away and off centre. A resumed
        # follow controller would now walk/turn instead of holding for speech.
        tracker.box = [450, 150, 490, 260]
        first = len(dog.sent)
        await sleep(0.15)
        for command_id, name, args in [("say-next", "say", {"text": "How are you?"}),
                                        ("listen-next", "listen", {"max_s": 1})]:
            assert view.missions.submit({"command_id": command_id, "name": name, "args": args})[0] == 202
            while view.missions.get(command_id)["state"] != "completed":
                assert not task.done()
                await sleep(0.005)
            await sleep(0.1)
        assert view.state["tracks"], "hold must work with visible people"
        moves = [json.loads(data["parameter"]) for topic, data, _ in dog.sent[first:]
                 if topic == patrol.TOPIC_SPORT]
        assert moves and all(move["x"] == move["z"] == 0 for move in moves)

    run_social_case(monkeypatch, tmp_path,
                    {"name": "find_person", "args": {"name": None, "timeout_s": 5, "approach": True}},
                    autonomous=True, tracker=tracker, after_submit=check_hold)


def test_blocked_follow_eventually_ignores_greeted_person(monkeypatch, tmp_path):
    policy = patrol.GreetPolicy()
    policy.greeted[4] = float("inf")
    tracker = MovingPerson()
    tracker.box = [300, 150, 340, 260]
    # At 0.8 m the follow controller proposes forward movement, but the final
    # guard uses the planner's 1.0 m stop distance and sends zero velocity.
    planner = patrol.PatrolPlanner(stop_m=1.0, leash_m=50.0)
    report, dog, _ = run_social_case(
        monkeypatch, tmp_path, None, autonomous=True, tracker=tracker,
        dog=FakeDog(wall_x=0.8), duration=9.5, policy=policy, planner=planner)
    assert report["modes"].get("follow", 0) > 0
    assert 4 in policy.ignored, "guarded zero velocity must advance the stationary-follow timer"
    assert not hello_requests(dog)


def test_unaddressed_conversation_cannot_enqueue_movement(monkeypatch, tmp_path):
    phrase = "I remember the way you sit on the chair"
    reply = "That sounds like a comfortable spot."
    spoken, conversed = [], []
    original_speak = MockAudio.speak

    def record_speak(self, text):
        spoken.append(text)
        return original_speak(self, text)

    def converse(text, situation, *, who, inference):
        assert inference is None
        conversed.append(text)
        return {"text": reply, "kind": "chat", "source": "test"}

    def forbidden_submit(self, payload):
        pytest.fail(f"unaddressed conversation enqueued a mission: {payload}")

    class FakeVoice:
        on_command = None
        started = stopped = False

        def start(self):
            self.started = True
            self.on_command({"intent": "converse", "phrase": phrase}, phrase)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(MockAudio, "speak", record_speak)
    monkeypatch.setattr(patrol.agent_mod, "converse_reply", converse)
    monkeypatch.setattr(patrol.MissionBoard, "submit", forbidden_submit)
    voice = FakeVoice()
    report, dog, _ = run_social_case(monkeypatch, tmp_path, None, voice=voice)
    assert voice.started and voice.stopped
    assert conversed == [phrase] and spoken == [reply]
    assert report["paused"] and report["missions"] == []
    assert report["conversations"][0]["reply"] == reply
    sport_requests = [options["api_id"] for topic, options in dog.requests
                      if topic == patrol.TOPIC_SPORT]
    assert all(api_id == patrol.STOP_MOVE for api_id in sport_requests), sport_requests
    moves = [json.loads(data["parameter"]) for topic, data, _ in dog.sent
             if topic == patrol.TOPIC_SPORT]
    assert moves and all(move["x"] == move["z"] == 0 for move in moves)
