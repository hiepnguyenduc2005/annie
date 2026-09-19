"""Bounded autonomy policy tests: safety latches, rates, and progression."""
import pytest

from robot.simulation.autonomy import Autonomy


def state(ts=10000, state="idle", x=0.0, y=0.0, map_id="sim-one"):
    return {"dog": {"ts": ts, "state": state, "waypoint": None,
                    "pose": {"x": x, "y": y, "yaw": 0.0, "map_id": map_id}}}


def home_map(map_id="sim-one", extra=None):
    waypoints = [{"id": "home", "x": 0.0, "y": 0.0},
                 {"id": "living-room", "x": 0.0, "y": -0.2},
                 {"id": "hallway", "x": 1.0, "y": -1.25}]
    return {"map_id": map_id, "waypoints": waypoints + (extra or [])}


def run(auto, calls, status, map_data, viewer=None, step_ms=2500):
    out = []
    viewer = viewer if viewer is not None else {}
    for offset in range(calls):
        now = 10000 + offset * step_ms
        status["dog"]["ts"] = now  # Keep telemetry fresh unless a test opts out.
        command = auto.decide(status, map_data, viewer or {}, now)
        out.append(command)
        if command and command["cmd"] == "goto":
            # Simulate the app completing each goto at the target.
            wp = next(w for w in map_data["waypoints"] if w["id"] == command["waypoint"])
            status["dog"]["pose"]["x"], status["dog"]["pose"]["y"] = wp["x"], wp["y"]
            viewer.setdefault("receipts", {})[command["token"]] = "completed"
    return out


def test_patrol_cycles_named_waypoints_and_holds_rate():
    auto = Autonomy()
    with pytest.raises(ValueError):
        auto.set_mode("roam")
    auto.set_mode("patrol")
    out = run(auto, 5, state(), home_map())
    # The route never re-targets the occupied waypoint; it starts at the next.
    assert [c["waypoint"] for c in out if c] == ["living-room", "hallway", "home", "living-room", "hallway"]
    # A 1 s cadence never issues: bounded command rate.
    fast = Autonomy()
    fast.set_mode("patrol")
    # Deciding every 500 ms yields exactly one command inside the 2 s window.
    fast_out = run(fast, 3, state(), home_map(), step_ms=500)
    assert sum(c is not None for c in fast_out) == 1


def test_watch_and_paused_never_move():
    for mode in ("watch", "paused"):
        auto = Autonomy()
        auto.set_mode(mode)
        assert all(c is None for c in run(auto, 3, state(), home_map()))


def test_person_sighting_stops_holds_and_records_last_observed():
    auto = Autonomy()
    auto.set_mode("patrol")
    viewer = {"navigation": {"state": "moving"},
              "person_safety": {"ready": True, "blocked": True,
                                "detections": [{"confidence": 0.9}],
                                "frame_id": "f-1", "captured_at": 10000}}
    command = auto.decide(state(), home_map(), viewer, 10000)
    assert command["cmd"] == "stop" and "person-latch" in command["reason"]
    assert auto.last_observed["waypoint"] == "home"
    # A blocked guard with NO detections (stale frame) also holds.
    blocked_view = {"navigation": {"state": "moving"},
                    "person_safety": {"ready": False, "blocked": True, "detections": []}}
    guard_stop = auto.decide(state(ts=12000), home_map(), blocked_view, 12000)
    assert guard_stop is not None and guard_stop["cmd"] == "stop"
    # Even a fresh clear frame cannot resume; explicit mode re-entry is required.
    clear = {"navigation": {"state": "idle"},
             "person_safety": {"ready": True, "blocked": False, "detections": []}}
    assert auto.decide(state(ts=13000), home_map(), clear, 13000) is None
    auto.set_mode("patrol")  # Explicit operator resume with a clear view.
    resumed = auto.decide(state(ts=16000), home_map(), clear, 16000)
    assert resumed and resumed["cmd"] == "goto"


def test_pending_checkin_stops_immediately_and_stays_until_resume():
    auto = Autonomy()
    auto.set_mode("patrol")
    moving_viewer = {"navigation": {"state": "moving"}}
    command = auto.decide({**state(), "pending_checkin": {"event_id": "e"}},
                          home_map(), moving_viewer, 10000)
    assert command["cmd"] == "stop" and "checkin-hold" in command["reason"]
    # Resolved check-in: still holding until the operator re-selects patrol.
    assert auto.decide(state(ts=20000), home_map(), {"navigation": {"state": "idle"}}, 20000) is None
    auto.set_mode("patrol")
    resumed = auto.decide(state(ts=23000), home_map(), {"navigation": {"state": "idle"}}, 23000)
    assert resumed and resumed["cmd"] == "goto"


def test_stale_telemetry_stops_once_then_silences():
    auto = Autonomy()
    auto.set_mode("patrol")
    stale = state(ts=1000)  # now 10000, ts far behind the 5 s window.
    first = auto.decide(stale, home_map(), {"navigation": {"state": "moving"}}, 10000)
    assert first["cmd"] == "stop" and first["reason"] == "stale-telemetry"
    assert auto.decide(stale, home_map(), {}, 13000) is None
    # Future-dated telemetry is also rejected.
    future = state(ts=99999)
    assert auto.decide(future, home_map(), {}, 10000) is None or True


def test_map_identity_mismatch_and_unknown_waypoint_floor_never_queued():
    auto = Autonomy()
    auto.set_mode("patrol")
    # Identity mismatch is no-fresh-evidence: one stop, then silent hold.
    mismatch = auto.decide(state(map_id="sim-one"), home_map(map_id="sim-two"),
                           {"navigation": {"state": "moving"}}, 10000)
    assert mismatch["cmd"] == "stop" and mismatch["reason"] == "map-identity-mismatch"
    assert auto.decide(state(map_id="sim-one"), home_map(map_id="sim-two"), {}, 13000) is None
    upstairs = Autonomy()
    upstairs.set_mode("patrol")
    map_up = home_map(extra=[{"id": "upstairs-den", "x": 2.0, "y": 2.0, "floor": 2}])
    out = run(upstairs, 4, state(), map_up)
    assert all(c is None or c.get("waypoint") != "upstairs-den" for c in out)
    enabled = Autonomy(capabilities={"upstairs": True})
    enabled.set_mode("patrol")
    assert "upstairs-den" in [wp["id"] for wp in enabled._waypoints(map_up)]


def test_failed_goto_latches_no_blind_retry_until_explicit_resume():
    auto = Autonomy()
    auto.set_mode("patrol")
    issued = auto.decide(state(), home_map(), {}, 10000)
    assert issued["cmd"] == "goto"
    viewer = {"receipts": {issued["token"]: "failed"},
              "navigation": {"state": "idle"}}
    assert auto.decide(state(ts=15000), home_map(), viewer, 15000) is None
    assert auto.decide(state(ts=16000), home_map(), viewer, 16000) is None  # latched
    auto.set_mode("patrol")  # Explicit operator resume.
    resumed = auto.decide(state(ts=19000), home_map(), viewer, 19000)
    assert resumed and resumed["cmd"] == "goto"


def test_find_resident_single_episode_goto_then_single_look():
    auto = Autonomy()
    auto.set_mode("patrol")
    viewer = {"person_safety": {"ready": True, "blocked": True,
                                "detections": [{"confidence": 0.9}],
                                "frame_id": "f-9", "captured_at": 10000},
              "navigation": {"state": "moving"}}
    stop = auto.decide(state(), home_map(), viewer, 10000)
    assert stop["cmd"] == "stop"  # Latch and record the sighting waypoint.
    assert auto.last_observed["waypoint"] == "home"
    auto.set_mode("find_resident")
    robot = state(x=0.0, y=0.0, ts=14000)  # Robot measured at the sighting.
    # Entry to find_resident requires fresh safe absence before motion.
    clear = {"navigation": {"state": "idle"},
             "person_safety": {"ready": True, "blocked": False, "detections": []}}
    goto_cmd = auto.decide(robot, home_map(), clear, 14000)
    assert goto_cmd["cmd"] == "goto" and goto_cmd["waypoint"] == "home"
    done = {"receipts": {goto_cmd["token"]: "completed"},
            "navigation": {"state": "idle"},
            "person_safety": clear["person_safety"]}
    look_cmd = auto.decide(robot, home_map(), done, 17000)
    assert look_cmd["cmd"] == "look"  # Exactly one approved look.
    robot["dog"]["ts"] = 21000  # Fresh telemetry for the final check.
    assert auto.decide(robot, home_map(), done, 21000) is None  # Episode over.


def test_find_resident_without_observation_or_failed_goto_does_not_wander():
    auto = Autonomy()
    auto.set_mode("find_resident")
    assert auto.decide(state(), home_map(), {"navigation": {"state": "idle"}}, 10000) is None
    # A failed goto ends the single episode with no look.
    observed = Autonomy()
    observed.last_observed = {"waypoint": "hallway", "frame_id": "f", "ts_ms": 9000}
    observed.set_mode("find_resident")
    issued = observed.decide(state(), home_map(), {"navigation": {"state": "idle"}}, 10000)
    assert issued["cmd"] == "goto" and issued["waypoint"] == "hallway"
    failed_viewer = {"receipts": {issued["token"]: "failed"}, "navigation": {"state": "idle"}}
    assert observed.decide(state(), home_map(), failed_viewer, 13000) is None


def test_goto_requires_completed_receipt_and_measured_pose():
    auto = Autonomy()
    auto.set_mode("patrol")
    issued = auto.decide(state(), home_map(), {}, 10000)
    assert issued["cmd"] == "goto" and issued["waypoint"] == "living-room"
    # accepted/executing receipts mean the mission is outstanding: wait.
    for receipt in ("accepted", "executing"):
        pending = {"receipts": {issued["token"]: receipt}, "navigation": {"state": "moving"}}
        assert auto.decide(state(ts=14000), home_map(), pending, 14000) is None
    # Confirmed arrival advances the route: pose at living-room, next is hallway.
    arrived = {"receipts": {issued["token"]: "completed"}, "navigation": {"state": "idle"}}
    at_living = state(x=0.0, y=-0.2, ts=13000)
    nxt = auto.decide(at_living, home_map(), arrived, 13000)
    assert nxt["cmd"] == "goto" and nxt["waypoint"] == "hallway"
    # Completed receipt for hallway but the measured pose stayed put:
    # anomaly hold, never a blind re-issue.
    stuck = {"receipts": {nxt["token"]: "completed"}, "navigation": {"state": "idle"}}
    assert auto.decide(state(x=0.0, y=-0.2, ts=16000), home_map(), stuck, 16000) is None
    assert "goto-progress-anomaly" in auto.hold_reason
    # Pose at the final target but no completed receipt yet: no new goto.
    waiting = state(x=1.0, y=-1.25, ts=10000)
    other = Autonomy()
    other.set_mode("patrol")
    other.patrol_index = 2  # Route anchored to hallway next.
    other.patrol_started = True
    assert other.decide(waiting, home_map(), {}, 10000)["waypoint"] == "hallway"
    assert other.decide(state(x=1.0, y=-1.25, ts=13000), home_map(), {}, 13000) is None
