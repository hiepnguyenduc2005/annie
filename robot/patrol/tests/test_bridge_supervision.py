"""Exercise the real bridge with delayed/failed models and changing state.

Mock HTTP only: these checks never contact a model, a server or hardware.
"""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from robot.simulation.bridge import Bridge


@pytest.mark.parametrize("fault", [
    "expired_plan_fresh_camera", "model_timeout", "pause", "goal_change",
    "map_change", "camera_stale", "active_motion", "active_receipt", "none",
    "expires_during_command_check", "pause_during_command_check",
])
def test_no_new_motion_after_async_failure_or_context_change(fault):
    async def exercise():
        now = [100.0]
        state = {
            "map_id": "sim-1", "ready": True, "running": True,
            "physics_error": None, "intelligence_enabled": True,
            "intelligence_revision": 1, "intelligence_goal": "Visit kitchen",
            "navigation": {"state": "idle", "waypoints": [
                {"id": "kitchen", "x": 1.0, "y": 0.0}], "commands": []},
            "person_safety": {"ready": True, "blocked": False, "captured_at": 100000},
            "resident_guard": {"blocked": False}, "speech": [],
        }
        initial = deepcopy(state)
        observation = {
            "frame_id": str(uuid4()), "ts": 100000,
            "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0, "map_id": "sim-1"},
            "source": "simulation_render", "jpeg_b64": "SYNTHETIC_TEST_ONLY",
        }
        commands = []
        ingests = []
        plan_calls = []
        existing = [{"command_id": str(uuid4()), "cmd": "goto", "waypoint": "kitchen",
                     "status": "accepted"}] if fault == "active_receipt" else []

        async def handler(request):
            path = request.url.path
            if path == "/observation":
                return httpx.Response(200, json=observation)
            if path == "/plan":
                plan_calls.append(request)
                now[0] = 107.0 if fault == "expired_plan_fresh_camera" else 100.2
                state["person_safety"]["captured_at"] = int(now[0] * 1000)
                if fault == "model_timeout":
                    raise httpx.ReadTimeout("mock provider timed out", request=request)
                if fault == "pause":
                    state["intelligence_enabled"] = False
                if fault == "goal_change":
                    state["intelligence_revision"] += 1
                if fault == "map_change":
                    state["map_id"] = "sim-2"
                if fault == "camera_stale":
                    state["person_safety"].update(ready=False, blocked=True, captured_at=95000)
                if fault == "active_motion":
                    state["navigation"]["state"] = "moving"
                return httpx.Response(200, json={
                    **{key: observation[key] for key in ("frame_id", "ts", "pose")},
                    "perception": {"person": False, "posture": "unknown", "location": "unknown",
                                   "confidence": 0.8, "caption": "Empty room"},
                    "action": {"action": "goto", "waypoint_id": "kitchen", "reason": "Continue patrol"},
                    "provider": {"mode": "local", "model": "mock-only"},
                    "latency_ms": (now[0] - 100) * 1000,
                })
            if path == "/state":
                return httpx.Response(200, json=state)
            if path == "/status":
                return httpx.Response(200, json={"pending_checkin": None,
                    "dog": {"state": "idle", "ts": int(now[0] * 1000)}})
            if path == "/commands" and request.method == "GET":
                if fault == "expires_during_command_check":
                    now[0] = 107.0
                if fault == "pause_during_command_check":
                    state["intelligence_enabled"] = False
                return httpx.Response(200, json=existing)
            if path == "/commands" and request.method == "POST":
                commands.append(json.loads(request.content))
                return httpx.Response(200, json={"command_id": str(uuid4()), "status": "queued"})
            if path == "/ingest":
                ingests.append(json.loads(request.content))
                return httpx.Response(200, json={"accepted": fault != "expired_plan_fresh_camera"})
            if path == "/query":
                return httpx.Response(200, json={"citations": []})
            if path == "/recall":
                return httpx.Response(200, json={"citations": [], "provider": "lexical_fallback"})
            if path == "/events":
                return httpx.Response(200, json=[])
            raise AssertionError(f"Unexpected mocked request: {request.method} {path}")

        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
            bridge = Bridge(client, client, client, perception="agent", clock=lambda: now[0])
            bridge.map_id = "sim-1"
            bridge.command_statuses = {item["command_id"]: item["status"] for item in existing}
            await bridge.think(initial)
        assert len(plan_calls) == 1, "The regression must reach the actual model/decision path"
        if fault == "none":
            assert commands == [{"cmd": "goto", "waypoint": "kitchen"}]
        else:
            assert not commands, f"Bridge queued motion after {fault}: {commands}"
        if fault == "model_timeout":
            assert not ingests
        for item in ingests:
            assert item["data"]["ts"] == observation["ts"]
            assert "jpeg_b64" not in item["data"]

    asyncio.run(exercise())
