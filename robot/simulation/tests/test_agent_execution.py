"""Execution-gate tests for model-selected actions (bridge.think contract).

State and status shapes mirror what Bridge.think actually passes:
state is the live viewer /state snapshot; status is the app GET /status;
frame is the /observation slice sent to /plan. No network, no paid calls.
"""
import pytest

from robot.simulation.agent_execution import gate_action


def base_state(**overrides):
    state = {
        "map_id": "sim-one",
        "running": True,
        "physics_error": False,
        "intelligence_enabled": True,
        "navigation": {"state": "idle", "waypoints": [
            {"id": "home", "x": 0.0, "y": 0.0},
            {"id": "hallway", "x": 1.0, "y": -1.25},
        ]},
        "person_safety": {"ready": True, "blocked": False, "detections": []},
        "resident_guard": {"blocked": False},
        "speech": [],
    }
    state.update(overrides)
    return state


def base_status():
    return {"dog": {"ts": 10000, "state": "idle"}, "pending_checkin": None}


def frame(map_id="sim-one", ts=9500):
    return {"frame_id": "f-1", "ts": ts, "pose": {"x": 0.0, "y": 0.0, "map_id": map_id}}


def gate(action, **overrides):
    kwargs = dict(state=base_state(), status=base_status(), frame=frame(),
                  now_ms=10000, last_speech_ms=None)
    kwargs.update(overrides)
    return gate_action(action, **kwargs)


def test_valid_goto_known_waypoint_accepted():
    command, detail = gate({"action": "goto", "waypoint_id": "hallway"})
    assert command == {"cmd": "goto", "waypoint": "hallway"}
    assert "accepted" in detail


def test_valid_look_accepted():
    command, detail = gate({"action": "look"})
    assert command == {"cmd": "look"}


def test_finish_completes_goal_without_a_motor_command():
    assert gate({"action": "finish"}) == (None, "Model completed the goal")
    # Finished audio and a recent speech cooldown do not imply pending work.
    state = base_state(speech=[{"status": "completed"}])
    assert gate({"action": "finish"}, state=state, last_speech_ms=9900) == (
        None, "Model completed the goal")


@pytest.mark.parametrize("nav_state", ["moving", "scanning", "turning"])
def test_finish_rejects_pending_motion(nav_state):
    state = base_state(navigation={"state": nav_state, "waypoints": []})
    command, detail = gate({"action": "finish"}, state=state)
    assert command is None
    assert detail == "Cannot finish while current motion has no terminal execution receipt"


@pytest.mark.parametrize("clip_status", ["queued", "generated", "playing"])
def test_finish_rejects_pending_audio(clip_status):
    state = base_state(speech=[{"status": clip_status}])
    assert gate({"action": "finish"}, state=state) == (
        None, "Cannot finish while audio is pending or playing")


def test_finish_rejects_pending_checkin():
    status = {**base_status(), "pending_checkin": {"event_id": "e-1"}}
    assert gate({"action": "finish"}, status=status) == (
        None, "Incident check-in owns motion and speech until resolved")


@pytest.mark.parametrize("ts", [4999, 10001])
def test_finish_rejects_stale_or_future_capture(ts):
    assert gate({"action": "finish"}, frame=frame(ts=ts)) == (
        None, "Model decision expired before execution")


def test_finish_rejects_changed_map():
    assert gate({"action": "finish"}, state=base_state(map_id="sim-two")) == (
        None, "Scene changed while the model was thinking")


@pytest.mark.parametrize("override", [{"running": False}, {"physics_error": True}])
def test_finish_rejects_paused_or_faulted_simulation(override):
    assert gate({"action": "finish"}, state=base_state(**override)) == (
        None, "Simulation is paused or faulted")


def test_expired_plan_frame_is_rejected():
    # Frame is 6 s old relative to now_ms: decision expired.
    assert gate({"action": "goto", "waypoint_id": "hallway"},
                frame=frame(ts=3000)) == (None, "Model decision expired before execution")
    # Future-dated frame is also outside the freshness window.
    assert gate({"action": "goto", "waypoint_id": "hallway"},
                frame=frame(ts=20000))[0] is None


def test_map_change_rejects_motion_and_stop_alike():
    state = base_state(map_id="sim-two")
    for action in ({"action": "goto", "waypoint_id": "hallway"}, {"action": "stop"}):
        command, detail = gate(action, state=state, frame=frame(map_id="sim-one"))
        assert command is None
        assert detail == "Scene changed while the model was thinking"


def test_paused_or_faulted_simulation_holds_motion():
    for override in ({"running": False}, {"physics_error": True}):
        state = base_state(**override)
        command, detail = gate({"action": "goto", "waypoint_id": "hallway"}, state=state)
        assert command is None and detail == "Simulation is paused or faulted"


def test_pending_checkin_owns_motion_and_speech():
    status = {**base_status(), "pending_checkin": {"event_id": "e-1"}}
    for action in ({"action": "goto", "waypoint_id": "hallway"},
                   {"action": "look"}, {"action": "say", "text": "All okay?"}):
        command, detail = gate(action, status=status)
        assert command is None
        assert "check-in owns motion and speech" in detail


def test_person_or_stale_camera_holds_motion_but_not_stop():
    for safety in ({"ready": False, "blocked": True, "detections": []},
                   {"ready": True, "blocked": True, "detections": [{"c": 0.9}]}):
        state = base_state(person_safety=safety)
        command, detail = gate({"action": "goto", "waypoint_id": "hallway"}, state=state)
        assert command is None
        assert "interlock holds motion" in detail
        # Stop is always allowed through the interlock.
        assert gate({"action": "stop"}, state=state)[0] == {"cmd": "stop"}
    state = base_state(resident_guard={"blocked": True})
    assert gate({"action": "look"}, state=state)[0] is None


def test_no_new_motion_while_command_executing():
    for nav_state in ("moving", "scanning", "turning"):
        state = base_state(**{"navigation": {"state": nav_state, "waypoints": [
            {"id": "home", "x": 0.0, "y": 0.0},
            {"id": "hallway", "x": 1.0, "y": -1.25},
        ]}})
        command, detail = gate({"action": "goto", "waypoint_id": "hallway"}, state=state)
        assert command is None
        assert "no terminal execution receipt" in detail
    # An unknown waypoint is rejected on its own terms.
    assert gate({"action": "goto", "waypoint_id": "attic"})[0] is None


def test_stop_preempts_every_hold():
    holds = {
        "paused": base_state(running=False),
        "checkin": base_state(),
        "person": base_state(person_safety={"ready": False, "blocked": True, "detections": []}),
    }
    status = {**base_status(), "pending_checkin": {"event_id": "e-1"}}
    assert gate({"action": "stop"}, state=holds["paused"])[0] == {"cmd": "stop"}
    assert gate({"action": "stop"}, state=holds["checkin"], status=status)[0] == {"cmd": "stop"}
    assert gate({"action": "stop"}, state=holds["person"])[0] == {"cmd": "stop"}
    # Wait and unknown actions are refused, never executed.
    assert gate({"action": "wait"})[0] is None
    assert gate({"action": "dance"})[1] == "Unknown model action"


def test_speech_cooldown_and_queued_audio_gate_say():
    text = {"action": "say", "text": "Everything looks fine."}
    # Recent speech blocks a repeat inside the 15 s cooldown.
    assert gate(text, last_speech_ms=9000)[0] is None
    # A clip already queued, playing, or generated blocks new audio.
    for clip_status in ("queued", "playing", "generated"):
        state = base_state(speech=[{"status": clip_status}])
        assert gate(text, state=state)[0] is None
    # Fresh, quiet pipeline accepts the speech command.
    command, detail = gate(text)
    assert command == {"cmd": "say", "text": "Everything looks fine."}
    # Blank, non-string, and oversized text are refused.
    assert gate({"action": "say", "text": "   "})[0] is None
    assert gate({"action": "say", "text": 5})[0] is None
    assert gate({"action": "say", "text": "x" * 501})[0] is None


def test_trick_uses_motion_and_incident_gates():
    action = {'action': 'trick', 'trick': 'spin'}
    assert gate(action)[0] == {'cmd': 'trick', 'trick': 'spin'}
    assert gate(action, status={'pending_checkin': {'event_id': 'e'}})[0] is None
    assert gate(action, state=base_state(person_safety={'ready': False}))[0] is None
    assert gate(action, frame=frame(ts=1))[0] is None
    assert gate({'action': 'trick', 'trick': 'jump'})[0] is None
    for kind in ('goto', 'look', 'trick', 'finish'):
        assert gate({'action': kind, 'trick': 'spin', 'waypoint_id': 'home'},
                    state=base_state(navigation={'state': 'tricking'}))[0] is None
