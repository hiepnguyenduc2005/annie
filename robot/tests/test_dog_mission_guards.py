"""Mission safety regressions using fake transport only; no hardware/providers/audio."""
import asyncio
import json
import time
import copy

import pytest

from test_go2_patrol_greet_runtime import FakeDog, NobodyTracker, StraightBandit
from robot.dog.runtime import patrol
from robot.dog.sim_audio import MockAudio


def scenario(monkeypatch, name="walk", args=None, *, fault=None, duration=0.45, **options):
    dog = FakeDog(step_m=0.001)
    view = patrol.LiveView(port=0)
    old_subscribe = dog.datachannel.pub_sub.subscribe
    triggered = [False]

    def subscribe(topic, callback):
        def filtered(message):
            moving = any(abs(json.loads(data["parameter"])[axis]) > 0
                         for t, data, _ in dog.sent if t == patrol.TOPIC_SPORT for axis in ("x", "z"))
            if moving:
                triggered[0] = True
                if fault == "pose" and topic == "rt/utlidar/robot_pose":
                    return
                if fault == "lowstate" and topic == "rt/lf/lowstate":
                    return
                if fault == "lidar" and topic == patrol.TOPIC_VOXELS:
                    return
                if fault == "battery" and topic == "rt/lf/lowstate":
                    from test_go2_patrol_greet_runtime import LOWSTATE
                    message = copy.deepcopy(LOWSTATE)
                    message["data"]["bms_state"]["soc"] = 10
                if fault == "boundary" and topic == "rt/utlidar/robot_pose":
                    from test_go2_patrol_greet_runtime import pose_msg
                    message = pose_msg(200, 0)
            callback(message)
        old_subscribe(topic, filtered)
    dog.datachannel.pub_sub.subscribe = subscribe

    async def run():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="10.0.0.99", aes_key=None, conn_factory=lambda *_: dog, tracker=NobodyTracker(),
            encoder=lambda frame: (b"", 640, 480), speak=lambda text: None, status=lambda text: None,
            rate_hz=200, stale_s=0.08, lidar_stale_s=0.08, boundary_m=100,
            voxel_min_interval_s=0, bandit=StraightBandit(), frontier_planner=None,
            view=view, duration_s=duration, idle_trick_s=0, **options))
        while not hasattr(view, "missions"):
            await asyncio.sleep(0.001)
        code, receipt = view.missions.submit({"command_id": "guard-test", "name": name,
                                              "args": {"metres": 1} if args is None else args})
        assert code == 202
        if fault == "inference_stop":
            while view.missions.get("guard-test")["state"] != "executing":
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.03)
            view.missions.submit({"command_id": "stop-test", "name": "stop", "args": {}})
            index = len(dog.sent)
        elif fault == "stop":
            while not any(abs(json.loads(d["parameter"])["x"]) + abs(json.loads(d["parameter"])["z"]) > 0
                          for t, d, _ in dog.sent if t == patrol.TOPIC_SPORT):
                await asyncio.sleep(0.001)
            view.missions.submit({"command_id": "stop-test", "name": "stop", "args": {}})
            index = len(dog.sent)
        else:
            index = None
        report = await task
        return report, view.missions.get("guard-test"), dog, index
    return asyncio.run(run())


@pytest.mark.parametrize("name,args", [("walk", {"metres": 1}), ("turn", {"degrees": 90})])
def test_stop_cancels_nested_motion_and_holds_neutral(monkeypatch, name, args):
    report, receipt, dog, index = scenario(monkeypatch, name, args, fault="stop")
    assert receipt["state"] == "cancelled"
    later = [json.loads(d["parameter"]) for t, d, _ in dog.sent[index:] if t == patrol.TOPIC_SPORT]
    assert later and all(p["x"] == p["z"] == 0 for p in later)
    assert dog.disconnected


@pytest.mark.parametrize("fault,error", [("pose", "telemetry_stale"), ("lowstate", "telemetry_stale"),
                                         ("battery", "battery_low"), ("boundary", "boundary_exceeded"),
                                         ("lidar", "lidar_stale")])
def test_nested_walk_rechecks_safety(monkeypatch, fault, error):
    report, receipt, dog, _ = scenario(monkeypatch, fault=fault)
    assert receipt["state"] == "failed", receipt
    assert receipt["error"] == error
    assert json.loads(dog.sent[-1][1]["parameter"])["x"] == 0


@pytest.mark.parametrize("name,args", [("walk", {"metres": 1}), ("turn", {"degrees": 90})])
def test_duration_does_not_claim_target_reached(monkeypatch, name, args):
    report, receipt, _, _ = scenario(monkeypatch, name, args, duration=0.12)
    assert receipt["state"] == "failed"
    assert receipt["error"] == "duration_complete"


def test_no_motion_fails_movement_receipt(monkeypatch):
    _, receipt, dog, _ = scenario(monkeypatch, no_motion=True)
    assert receipt["state"] == "failed" and "no_motion" in receipt["error"]
    assert not any(t == patrol.TOPIC_SPORT for t, _, _ in dog.sent)


def test_forward_obstacle_fails_instead_of_completing(monkeypatch):
    from test_go2_patrol_greet_runtime import voxel_msg
    monkeypatch.setattr("test_go2_patrol_greet_runtime.voxel_msg", lambda _: voxel_msg(0.4))
    _, receipt, _, _ = scenario(monkeypatch)
    assert receipt["state"] == "failed" and receipt["error"] == "obstacle ahead"


def test_mock_audio_never_uses_global_audio(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("real audio must not be called")
    monkeypatch.setattr(patrol, "speak_blocking", forbidden)
    monkeypatch.setattr(patrol, "listen_blocking", forbidden)
    audio = MockAudio("A scripted reply")
    _, said, _, _ = scenario(monkeypatch, "say", {"text": "hello"}, audio=audio, source="simulation")
    _, heard, _, _ = scenario(monkeypatch, "listen", {"max_s": 1}, audio=audio, source="simulation")
    assert audio.spoken == ["hello"]
    assert said["result"]["source"] == heard["result"]["source"] == "simulation"
    assert heard["result"]["transcript"] == "A scripted reply"


def test_mock_audio_rejected_for_hardware():
    with pytest.raises(ValueError, match="requires simulation"):
        asyncio.run(patrol.run_patrol_greet(ip="unused", aes_key=None, audio=MockAudio()))


def test_slow_inference_stop_cannot_resume_motion(monkeypatch):
    from robot.dog.runtime import body
    original = body.validate_command
    monkeypatch.setattr(body, "validate_command", lambda p: (p["command_id"], "look_for", p["args"])
                        if p["name"] == "look_for" else original(p))
    def slow_spot(*args):
        time.sleep(0.2)
        return {"seen": True, "where": "centre"}
    monkeypatch.setattr(patrol.agent_mod, "spot", slow_spot)
    _, receipt, dog, index = scenario(monkeypatch, "look_for", {"thing": "door"}, fault="inference_stop")
    assert receipt["state"] == "cancelled"
    assert all(json.loads(d["parameter"])["x"] == json.loads(d["parameter"])["z"] == 0
               for t, d, _ in dog.sent[index:] if t == patrol.TOPIC_SPORT)


def test_walk_completion_requires_measured_distance(monkeypatch):
    _, receipt, _, _ = scenario(monkeypatch, args={"metres": 0.02})
    assert receipt["state"] == "completed", receipt
    assert receipt["result"]["walked_m"] >= 0.02


def test_turn_local_timeout_is_failed(monkeypatch):
    _, receipt, _, _ = scenario(monkeypatch, "turn", {"degrees": 15}, duration=2.8)
    assert receipt["state"] == "failed" and "turn timeout" in receipt["error"]


def test_odometry_stall_fails_walk(monkeypatch):
    from robot.dog.planning.smart_patrol import StallDetector
    _, receipt, _, _ = scenario(monkeypatch, stall=StallDetector(window_s=0.05, min_progress_m=1))
    assert receipt["state"] == "failed" and receipt["error"] == "odometry stalled"


def test_full_turn_is_not_a_zero_motion_success(monkeypatch):
    _, receipt, dog, _ = scenario(monkeypatch, "turn", {"degrees": 360}, duration=0.12)
    assert receipt["state"] == "failed"
    assert any(json.loads(d["parameter"])["z"] > 0 for t, d, _ in dog.sent if t == patrol.TOPIC_SPORT)


def test_stop_interrupts_trick_settlement(monkeypatch):
    report, receipt, dog, index = scenario(monkeypatch, "dance", {}, fault="inference_stop")
    assert receipt["state"] == "cancelled"
    assert report["reason"] == "duration_complete"
    assert dog.disconnected
    assert all(json.loads(d["parameter"])["x"] == json.loads(d["parameter"])["z"] == 0
               for t, d, _ in dog.sent[index:] if t == patrol.TOPIC_SPORT)


def test_trick_duration_expiry_fails_receipt(monkeypatch):
    report, receipt, _, _ = scenario(monkeypatch, "dance", {}, duration=0.12)
    assert report["reason"] == "duration_complete"
    assert receipt["state"] == "failed"


def test_setup_exception_is_reported_before_guard_initialization():
    def fail_connect(*args):
        raise RuntimeError("fake setup failure")
    report = asyncio.run(patrol.run_patrol_greet(
        ip="unused", aes_key=None, conn_factory=fail_connect, tracker=NobodyTracker(),
        encoder=lambda frame: (b"", 640, 480), speak=lambda text: None, status=lambda text: None,
        view=patrol.LiveView(port=0), frontier_planner=None))
    assert report["reason"] == "error:RuntimeError"
    assert not report["completed"]


def test_named_mission_never_falls_back_and_identity_cache_is_track_bound():
    cache = {}
    named = {"track_id": 1, "identity": {"name": "Jeanine"}, "box": [0, 0, 10, 10]}
    stranger = {"track_id": 2, "identity": None, "box": [0, 0, 10, 100]}
    assert patrol._mission_person_pool([named, stranger], "jeanine", cache, 0) == [named]
    assert patrol._mission_person_pool([stranger], "jeanine", cache, 1) == []
    flicker = dict(named, identity=None)
    assert patrol._mission_person_pool([flicker], "jeanine", cache, 1)[0]["identity"]["name"] == "Jeanine"
    assert patrol._mission_person_pool([flicker, stranger], "jeanine", cache, 26) == []


def test_conflicting_identity_cannot_reuse_previous_match():
    cache = {}
    patrol._mission_person_pool([{"track_id": 1, "identity": {"name": "Jeanine"}}], "jeanine", cache, 0)
    assert patrol._mission_person_pool([{"track_id": 1, "identity": {"name": "Alex"}}], "jeanine", cache, 1) == []


def test_simulation_faces_default_isolated(monkeypatch):
    view = patrol.LiveView(port=0)
    monkeypatch.delenv("ANNIE_FACES_DIR", raising=False)
    def fail_connect(*args):
        raise RuntimeError("fake setup failure")
    asyncio.run(patrol.run_patrol_greet(ip="unused", aes_key=None, source="simulation", view=view,
                                      conn_factory=fail_connect, tracker=NobodyTracker(), status=lambda text: None,
                                      frontier_planner=None))
    assert str(view.people.faces_dir) == ".data/sim/faces"


def test_http_action_stop_cancels_executing_mission_and_resume_is_explicit():
    import socket
    import urllib.request
    from robot.dog.planning.missions import MissionBoard
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    view = patrol.LiveView(port=port)
    view.missions = MissionBoard()
    view.commands = {}
    _, submitted = view.missions.submit({"name": "turn", "args": {"degrees": 90}})
    view.missions.take()
    url = view.start()
    def action(value):
        request = urllib.request.Request(url + "command", data=json.dumps({"action": value}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 202
    try:
        action("stop")
        assert view.missions.get(submitted["command_id"])["state"] == "cancelled"
        assert view.missions.stop_requested
        assert view.commands["resume_requested"] is False
        action("explore")
        assert view.commands["resume_requested"] is True
        assert view.commands["intent"] == "explore"
    finally:
        view.server.shutdown()
        view.server.server_close()


@pytest.mark.parametrize("autonomous_greeting", [False, True])
def test_stop_during_show_holds_and_new_mission_resumes_same_runtime(monkeypatch, autonomous_greeting):
    from test_go2_patrol_greet_runtime import PersonTracker_
    class Close(PersonTracker_):
        def update(self, jpeg, now_ms):
            return [{"track_id": 4, "box": [280, 60, 360, 330], "conf": 0.9,
                     "posture": "upright", "lying_frames": 0, "kp_conf": [0.9] * 17,
                     "first_seen_ms": now_ms - 1000}]
    original_sleep = asyncio.sleep
    async def quick_start(delay):
        await original_sleep(0.005 if delay == 1.5 else delay)
    monkeypatch.setattr(asyncio, "sleep", quick_start)
    monkeypatch.setattr(patrol.agent_mod, "compose_line", lambda *a, **kw: {"text": "Hello", "source": "test", "latency_ms": 0})
    dog, view, audio = FakeDog(), patrol.LiveView(port=0), MockAudio()
    async def run():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="unused", aes_key=None, conn_factory=lambda *_: dog,
            tracker=Close() if autonomous_greeting else NobodyTracker(),
            encoder=lambda frame: (b"", 640, 480), status=lambda text: None,
            rate_hz=200, stale_s=0.2, lidar_stale_s=0.2, boundary_m=100,
            voxel_min_interval_s=0, bandit=StraightBandit(), frontier_planner=None,
            view=view, duration_s=0.65, idle_trick_s=0, audio=audio, source="simulation"))
        while not hasattr(view, "missions"):
            await original_sleep(0.001)
        if not autonomous_greeting:
            view.missions.submit({"command_id": "dance-before-stop", "name": "dance"})
        expected_api = patrol.EULER if autonomous_greeting else 1022
        deadline = asyncio.get_running_loop().time() + 2
        while not any(options["api_id"] == expected_api for _, options in dog.requests):
            assert not task.done(), "runtime ended before beginning show"
            assert asyncio.get_running_loop().time() < deadline
            await original_sleep(0.005)
        view.missions.submit({"name": "stop"})
        index = len(dog.sent)
        await original_sleep(0.12)
        assert not task.done() and not dog.disconnected
        assert all(json.loads(d["parameter"])["x"] == json.loads(d["parameter"])["z"] == 0
                   for t, d, _ in dog.sent[index:] if t == patrol.TOPIC_SPORT)
        view.missions.submit({"command_id": "after-stop", "name": "say", "args": {"text": "Still here"}})
        while view.missions.get("after-stop")["state"] not in ("completed", "failed"):
            assert not task.done()
            await original_sleep(0.005)
        assert view.missions.get("after-stop")["state"] == "completed"
        assert "Still here" in audio.spoken
        # Hold again to avoid starting another unrelated greeting after the explicit mission.
        view.missions.submit({"name": "stop"})
        report = await task
        assert report["reason"] == "duration_complete"
        assert report["greetings"] == []  # interrupted greetings were never recorded as completed
        if not autonomous_greeting:
            assert view.missions.get("dance-before-stop")["state"] == "cancelled"
    asyncio.run(run())


def test_resident_policy_commits_only_dispatched_checkin_and_handles_new_track():
    policy = patrol.GreetPolicy()
    lying = {"track_id": 1, "posture": "lying", "lying_frames": 6,
             "box": [280, 120, 360, 340], "identity": {"name": "Jeanine", "method": "shirt_colour"}}
    places, radii = patrol._person_places([lying], 640, 480, (2, 3), 0, 1)
    assert places[1] == (3, 3)
    assert policy.step([lying], 640, 480, now_s=0, place_by_tid=places) == ("checkin", 1)
    assert policy.checked == {}  # a mission may suppress the selected action
    assert policy.step([lying], 640, 480, now_s=1, place_by_tid=places) == ("checkin", 1)
    policy.mark_checkin(lying, places[1], now_s=1, radius_m=radii[1])
    replacement = dict(lying, track_id=2)
    assert policy.step([replacement], 640, 480, now_s=2, place_by_tid={2: places[1]}) == ("patrol", None)
    other = dict(lying, track_id=3, identity={"name": "Alex", "method": "face"})
    assert policy.step([replacement, other], 640, 480, now_s=3, place_by_tid={2: places[1], 3: places[1]}) == ("checkin", 3)
    policy.observe([dict(lying, posture="upright")], places, now_s=4)
    assert policy.step([lying], 640, 480, now_s=5, place_by_tid=places) == ("checkin", 1)


def test_guest_alias_is_not_spoken_in_greeting():
    assert "Guest" not in patrol.GreetPolicy.greeting_text({"identity": {"name": "Guest 2", "method": "guest"}})


@pytest.mark.parametrize("kind", ["none", "target", "custom"])
def test_runtime_wires_shared_reid_but_preserves_custom_identifier(monkeypatch, tmp_path, kind):
    from robot.dog.perception.people import PeopleDirectory
    created, reid_options = [], []
    original = patrol.Perception
    def capture(*args, **kwargs):
        instance = original(*args, **kwargs)
        created.append(instance)
        return instance
    sentinel = object()
    def fake_reid(self, **kwargs):
        reid_options.append(kwargs)
        return sentinel
    monkeypatch.setattr(patrol, "Perception", capture)
    monkeypatch.setattr(PeopleDirectory, "reid", fake_reid)
    identifier = None if kind == "none" else patrol.TargetIdentifier("Jeanine", "red") if kind == "target" else object()
    view = patrol.LiveView(port=0)
    def fail_connect(*args):
        raise RuntimeError("fake setup failure")
    asyncio.run(patrol.run_patrol_greet(ip="unused", aes_key=None, source="simulation", view=view,
                                      conn_factory=fail_connect, tracker=NobodyTracker(), status=lambda text: None,
                                      frontier_planner=None, identifier=identifier, faces_dir=tmp_path))
    assert view.identifier is identifier
    if kind == "custom":
        assert created[0].identifier is identifier and not reid_options
    else:
        assert created[0].identifier is sentinel
        assert reid_options[0]["pose_source"]() is None
        assert reid_options[0]["shirt_rules"]() == ([] if identifier is None else [("Jeanine", "red")])
        if identifier is not None:
            identifier.colour = "blue"
            assert reid_options[0]["shirt_rules"]() == [("Jeanine", "blue")]


def test_no_motion_find_approach_fails_immediately(monkeypatch):
    report, receipt, dog, _ = scenario(monkeypatch, "find_person",
                                      {"name": "Jeanine", "timeout_s": 90, "approach": True},
                                      no_motion=True, duration=0.12)
    assert receipt["state"] == "failed" and receipt["error"] == "no_motion: movement disabled"
    assert receipt["finished_at_ms"] - receipt["started_at_ms"] < 100
    assert not any(t == patrol.TOPIC_SPORT for t, _, _ in dog.sent)


def test_start_paused_has_no_stand_and_read_only_mission_does_not_resume(monkeypatch, tmp_path):
    dog, view, audio = FakeDog(), patrol.LiveView(port=0), MockAudio()
    async def run():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="unused", aes_key=None, conn_factory=lambda *_: dog, tracker=NobodyTracker(),
            encoder=lambda frame: (b"", 640, 480), status=lambda text: None,
            view=view, duration_s=0.3, idle_trick_s=0, start_paused=True,
            source="simulation", audio=audio, faces_dir=tmp_path, frontier_planner=None))
        while not hasattr(view, "missions"):
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.08)
        snapshot = patrol.telemetry_snapshot(view)
        assert snapshot["motion_enabled"] and snapshot["paused"] and snapshot["source"] == "simulation"
        assert not any(o["api_id"] in (patrol.STAND_UP, patrol.BALANCE_STAND) for _, o in dog.requests)
        view.missions.submit({"command_id": "read-only", "name": "say", "args": {"text": "Waiting"}})
        while view.missions.get("read-only")["state"] != "completed":
            assert not task.done()
            await asyncio.sleep(0.005)
        assert view.report["paused"]
        report = await task
        assert report["paused"]
        assert all(json.loads(d["parameter"])["x"] == json.loads(d["parameter"])["z"] == 0
                   for t, d, _ in dog.sent if t == patrol.TOPIC_SPORT)
    asyncio.run(run())


def test_start_paused_explicit_motion_arms_and_executes(monkeypatch):
    original_sleep = asyncio.sleep
    async def quick_stand(delay):
        await original_sleep(0.005 if delay == 1.5 else delay)
    monkeypatch.setattr(asyncio, "sleep", quick_stand)
    report, receipt, dog, _ = scenario(monkeypatch, args={"metres": 0.02}, start_paused=True)
    assert receipt["state"] == "completed", receipt
    assert any(o["api_id"] == patrol.STAND_UP for _, o in dog.requests)
    assert not report["paused"]


@pytest.mark.parametrize("follow", [False, True])
def test_autonomous_forward_holds_on_lidar_loss_and_resumes_on_fresh_maps(monkeypatch, tmp_path, follow):
    from test_go2_patrol_greet_runtime import PersonTracker_
    original_sleep = asyncio.sleep
    async def quick_start(delay):
        await original_sleep(0.005 if delay == 1.5 else delay)
    monkeypatch.setattr(asyncio, "sleep", quick_start)
    dog, enabled, sent = FakeDog(step_m=0.001), [True], []
    original_subscribe = dog.datachannel.pub_sub.subscribe
    def subscribe(topic, callback):
        original_subscribe(topic, lambda message: callback(message) if topic != patrol.TOPIC_VOXELS or enabled[0] else None)
    dog.datachannel.pub_sub.subscribe = subscribe
    original_publish = dog.datachannel.pub_sub.publish_without_callback
    def publish(topic, data=None, msg_type=None):
        if topic == patrol.TOPIC_SPORT:
            sent.append((time.monotonic(), json.loads(data["parameter"])))
        original_publish(topic, data=data, msg_type=msg_type)
    dog.datachannel.pub_sub.publish_without_callback = publish
    async def run():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="unused", aes_key=None, conn_factory=lambda *_: dog,
            tracker=PersonTracker_() if follow else NobodyTracker(), encoder=lambda frame: (b"", 640, 480),
            speak=lambda text: None, status=lambda text: None, duration_s=0.65, rate_hz=200,
            stale_s=0.2, lidar_stale_s=0.05, voxel_min_interval_s=0, boundary_m=100,
            bandit=StraightBandit(), frontier_planner=None, idle_trick_s=0, source="simulation", faces_dir=tmp_path))
        while not any(data["x"] > 0 for _, data in sent):
            assert not task.done()
            await original_sleep(0.005)
        cut = time.monotonic()
        enabled[0] = False
        await original_sleep(0.22)
        blocked = [data for stamp, data in sent if stamp > cut + 0.09]
        assert blocked and all(data["x"] <= 0 for data in blocked)
        assert not task.done() and not dog.disconnected
        resumed = time.monotonic()
        enabled[0] = True
        report = await task
        assert any(data["x"] > 0 for stamp, data in sent if stamp > resumed + 0.08)
        assert report["reason"] == "duration_complete"
        assert not report["collisions"]  # inhibited proposals are not odometry stalls
    asyncio.run(run())


def test_follow_camera_obstacle_inhibits_forward_with_clear_lidar(monkeypatch, tmp_path):
    from test_go2_patrol_greet_runtime import PersonTracker_
    original = patrol.Perception
    class ObstaclePerception(original):
        def latest(self):
            current = super().latest()
            current["objects"] = [{"box": [150, 50, 490, 470], "hits": 3, "label": "chair"}]
            return current
    monkeypatch.setattr(patrol, "Perception", ObstaclePerception)
    dog = FakeDog()
    report = asyncio.run(patrol.run_patrol_greet(
        ip="unused", aes_key=None, conn_factory=lambda *_: dog, tracker=PersonTracker_(),
        encoder=lambda frame: (b"", 640, 480), speak=lambda text: None, status=lambda text: None,
        duration_s=0.15, stale_s=0.2, rate_hz=200, voxel_min_interval_s=0,
        frontier_planner=None, idle_trick_s=0, source="simulation", faces_dir=tmp_path))
    assert report["modes"].get("follow", 0) > 0
    assert not any(json.loads(data["parameter"])["x"] > 0 for topic, data, _ in dog.sent if topic == patrol.TOPIC_SPORT)


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
def test_cli_signal_unwinds_async_cleanup_even_with_inherited_ignored_int(tmp_path, signal_name):
    import signal
    import subprocess
    import sys
    script = r'''
import asyncio, signal, sys
from pathlib import Path
from types import SimpleNamespace
from robot.dog.runtime import patrol
signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.modules["robot.dog.sim"] = SimpleNamespace(SimDog=SimpleNamespace(
    from_cli=lambda scene: SimpleNamespace(connect_factory=lambda: None)))
patrol.SightingMemory = lambda *a, **kw: None
patrol.SpacetimeRecorder = None
async def fake_runtime(**kwargs):
    try:
        print("READY", flush=True)
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.01)
        Path(sys.argv[1]).write_text("async cleanup completed")
patrol.run_patrol_greet = fake_runtime
code = patrol.main(["--sim", "--mock-audio", "--view-port", "0", "--target", ""])
assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
raise SystemExit(code)
'''
    marker = tmp_path / "cleanup.txt"
    child = subprocess.Popen([sys.executable, "-c", script, str(marker)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "READY"
        child.send_signal(getattr(signal, signal_name))
        _, stderr = child.communicate(timeout=5)
        assert child.returncode == 130, stderr
        assert marker.read_text() == "async cleanup completed"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_deliberate_no_lidar_mode_still_allows_forward(monkeypatch):
    _, receipt, _, _ = scenario(monkeypatch, args={"metres": 0.02}, lidar=False)
    assert receipt["state"] == "completed", receipt


def test_simultaneous_unnamed_lying_tracks_are_not_merged_by_place():
    policy = patrol.GreetPolicy()
    first = {"track_id": 1, "posture": "lying", "lying_frames": 6, "box": [10, 10, 80, 100]}
    second = dict(first, track_id=2, box=[120, 10, 190, 100])
    places = {1: (0, 0), 2: (0.3, 0)}
    policy.mark_checkin(first, places[1], now_s=0)
    assert policy.step([first, second], 640, 480, now_s=1, place_by_tid=places) == ("checkin", 2)


@pytest.mark.parametrize("guarded_wait", [False, True])
@pytest.mark.parametrize("stop_code", [0, 7])
def test_stop_receipt_waits_for_actual_ack_in_idle_and_guarded_wait(monkeypatch, tmp_path, guarded_wait, stop_code):
    original_sleep = asyncio.sleep
    async def quick_start(delay):
        await original_sleep(0.005 if delay == 1.5 else delay)
    monkeypatch.setattr(asyncio, "sleep", quick_start)
    dog, view = FakeDog(), patrol.LiveView(port=0)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        submitted = False
        original_request = dog.datachannel.pub_sub.publish_request_new
        async def request(topic, options):
            result = await original_request(topic, options)
            if submitted and options["api_id"] == patrol.STOP_MOVE:
                entered.set()
                await release.wait()
                result["data"]["header"]["status"]["code"] = stop_code
            return result
        dog.datachannel.pub_sub.publish_request_new = request
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="unused", aes_key=None, conn_factory=lambda *_: dog, tracker=NobodyTracker(),
            encoder=lambda frame: (b"", 640, 480), speak=lambda text: None, status=lambda text: None,
            view=view, duration_s=0.5, rate_hz=200, start_paused=not guarded_wait, source="simulation",
            faces_dir=tmp_path, frontier_planner=None, idle_trick_s=0, voxel_min_interval_s=0))
        while not hasattr(view, "missions"):
            await original_sleep(0.001)
        if guarded_wait:
            view.missions.submit({"name": "dance", "command_id": "before-stop"})
            while not any(o["api_id"] == 1022 for _, o in dog.requests):
                assert not task.done()
                await original_sleep(0.005)
        submitted = True
        _, accepted = view.missions.submit({"name": "stop", "command_id": "ack-test"})
        assert accepted["state"] == "accepted" and accepted["stop_code"] is None
        await asyncio.wait_for(entered.wait(), 1)
        pending = view.missions.get("ack-test")
        assert pending["state"] == "executing" and pending["processed_at_ms"] is None
        assert pending["stop_code"] is None
        release.set()
        while view.missions.get("ack-test")["state"] == "executing":
            await original_sleep(0.005)
        receipt = view.missions.get("ack-test")
        assert receipt["state"] == ("completed" if stop_code == 0 else "failed")
        assert receipt["stop_code"] == receipt["result"]["stop_code"] == stop_code
        assert receipt["processed_at_ms"] >= receipt["started_at_ms"]
        assert not task.done() and not dog.disconnected  # stop acknowledgment keeps the server alive
        await task
    asyncio.run(run())


def test_paused_runtime_keeps_state_and_frame_annotations_live_without_motion(tmp_path):
    from test_go2_patrol_greet_runtime import PersonTracker_
    class AnnotationView(patrol.LiveView):
        def __init__(self):
            super().__init__(port=1)  # enable the real perception annotation callback, without binding HTTP
            self.annotations = []
        def start(self):
            return None
        def annotate(self, img, tracks, mode, ranges, battery, raw=None):
            self.annotations.append((time.monotonic(), mode, battery, list(tracks)))
    class SuppliedIdentity:
        def apply(self, img, tracks):
            return tracks
    dog, view, log = FakeDog(), AnnotationView(), []
    async def run():
        task = asyncio.create_task(patrol.run_patrol_greet(
            ip="unused", aes_key=None, conn_factory=lambda *_: dog, tracker=PersonTracker_(),
            identifier=SuppliedIdentity(), encoder=lambda frame: (b"", 640, 480),
            speak=lambda text: None, status=log.append, view=view, duration_s=0.35,
            start_paused=True, source="simulation", faces_dir=tmp_path, frontier_planner=None,
            rate_hz=100, voxel_min_interval_s=0, idle_trick_s=0))
        deadline = time.monotonic() + 2
        while view.state.get("t_s", 0) < 0.05 or not view.state.get("tracks"):
            assert not task.done() and time.monotonic() < deadline
            await asyncio.sleep(0.005)
        first = patrol.telemetry_snapshot(view)["state"]
        frames = len(view.annotations)
        await asyncio.sleep(0.07)
        later = patrol.telemetry_snapshot(view)["state"]
        assert later["t_s"] > first["t_s"]
        assert later["battery"] == 55 and later["pose"] == {"x": 0.0, "y": 0.0, "yaw": 0.0}
        assert later["tracks"][0]["track_id"] == 3
        assert later["mode"] == "paused" and later["action"] == "hold" and later["paused"]
        assert len(view.annotations) > frames
        assert any(mode == "paused" and battery == 55 and tracks for _, mode, battery, tracks in view.annotations)
        report = await task
        assert report["reason"] == "duration_complete" and report["paused"]
        assert not report["greetings"] and not report["checkins"]
        assert not any(options["api_id"] in (patrol.STAND_UP, patrol.BALANCE_STAND) for _, options in dog.requests)
        assert all(json.loads(data["parameter"])["x"] == json.loads(data["parameter"])["z"] == 0
                   for topic, data, _ in dog.sent if topic == patrol.TOPIC_SPORT)
        assert any("held/paused" in line for line in log)
    asyncio.run(run())
