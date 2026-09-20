"""Clothing assignment transport and motion holds: fake hardware only."""
import asyncio
import json
import socket
from types import SimpleNamespace
import urllib.error
import urllib.request

from robot.dog.runtime import patrol
from robot.dog.planning.missions import MissionBoard
from robot.dog.sim_audio import MockAudio
from test_dog_control_lifecycle import start_runtime, forward_moves
from test_go2_patrol_greet_runtime import FakeDog


def test_demo_casting_names_every_visible_person_without_enrollment():
    tracks = [{"track_id": 1, "box": [0, 0, 50, 100]},
              {"track_id": 2, "box": [50, 0, 100, 200], "identity": {"name": "Guest 2"}}]
    patrol.DemoEveryoneGrandma().apply(None, tracks)
    assert all(t["identity"] == {"name": "Jeanine", "method": "demo_role", "demo": True} for t in tracks)
    pool = patrol._mission_person_pool(tracks, "jeanine", {}, 0)
    assert len(pool) == 2
    assert max(pool, key=lambda t: t["box"][3] - t["box"][1])["track_id"] == 2


def test_demo_casting_forces_held_manual_start_without_selection(monkeypatch):
    dog, view = FakeDog(step_m=0.001), patrol.LiveView(port=0)

    async def run():
        task = await start_runtime(monkeypatch, dog, view, MockAudio(), manual=False, demo_everyone=True)
        report = await asyncio.wait_for(task, 5)
        assert report["paused"] and not forward_moves(dog)
        telemetry = patrol.telemetry_snapshot(view)
        assert telemetry["demo_everyone_grandma"]
        assert not telemetry["grandma_selection"]["enabled"]
        assert view.situation(0)["demo_everyone_grandma"]
    asyncio.run(run())


class Selection:
    def __init__(self, state="unselected"):
        self.state, self.calls = state, []

    def selection_status(self):
        return {"name": "Jeanine", "guest": "Guest 1", "state": self.state,
                "needs_selection": self.state != "tracking"}

    def assign_guest(self, guest, name="Jeanine"):
        self.calls.append((guest, name))
        if guest != "Guest 1":
            raise ValueError("Guest is no longer visible")
        self.state = "tracking"
        return self.selection_status()


def test_assignment_requires_token_paused_connected_and_live_guest():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    view, selected = patrol.LiveView(port=port, token="test-token"), Selection()
    view.require_grandma_selection = True
    view.people = SimpleNamespace(reid=lambda: selected, list=lambda: [])
    view.report = {"paused": True, "connection": {"status": "connected"}}
    view.missions = MissionBoard()
    url = view.start()

    def post(payload, token="test-token"):
        request = urllib.request.Request(url + "people/assign", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "X-Body-Token": token})
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    try:
        assert post({"guest": "Guest 1"}, token="wrong")[0] == 401
        assert not selected.calls
        view.report["paused"] = False
        assert post({"guest": "Guest 1"})[0] == 409
        view.report["paused"] = True
        view.report["connection"]["status"] = "disconnected"
        assert post({"guest": "Guest 1"})[0] == 503
        view.report["connection"]["status"] = "connected"
        assert post({"guest": "Guest 1", "name": "another name"})[0] == 409
        assert post({"guest": "Guest 999"})[0] == 409
        code, result = post({"guest": "Guest 1"})
        assert code == 200 and result["state"] == "tracking" and result["enabled"]
        assert patrol.telemetry_snapshot(view)["grandma_selection"]["state"] == "tracking"
    finally:
        view.server.shutdown()
        view.server.server_close()


def test_unselected_outfit_cannot_start_motion(monkeypatch):
    dog, view, selected = FakeDog(step_m=0.001), patrol.LiveView(port=0), Selection()

    async def run():
        task = await start_runtime(monkeypatch, dog, view, MockAudio(), require_selection=True)
        view.people = SimpleNamespace(reid=lambda: selected)
        view.missions.submit({"command_id": "walk-before-selection", "name": "walk", "args": {"metres": 1}})
        while view.missions.get("walk-before-selection")["state"] not in ("failed", "completed"):
            assert not task.done()
            await asyncio.sleep(0.002)
        receipt = view.missions.get("walk-before-selection")
        assert receipt["state"] == "failed" and "Select Grandma" in receipt["error"]
        report = await asyncio.wait_for(task, 5)
        assert report["paused"] and not forward_moves(dog)
    asyncio.run(run())


def test_ambiguous_outfit_stops_active_movement_and_clears_queue(monkeypatch):
    dog, view, selected = FakeDog(step_m=0.001), patrol.LiveView(port=0), Selection("tracking")

    async def run():
        task = await start_runtime(monkeypatch, dog, view, MockAudio(), require_selection=True)
        view.people = SimpleNamespace(reid=lambda: selected)
        view.missions.submit({"command_id": "patrol-before-ambiguity", "name": "patrol", "args": {"duration_s": 5}})
        while not forward_moves(dog):
            assert not task.done()
            await asyncio.sleep(0.002)
        view.missions.submit({"command_id": "queued-walk", "name": "walk", "args": {"metres": 1}})
        selected.state = "ambiguous"
        while view.missions.get("patrol-before-ambiguity")["state"] != "cancelled":
            assert not task.done()
            await asyncio.sleep(0.002)
        held = len(dog.sent)
        report = await asyncio.wait_for(task, 5)
        assert view.missions.get("queued-walk")["state"] == "cancelled"
        assert report["paused"] and not forward_moves(dog, held)
        assert any(options["api_id"] == patrol.STOP_MOVE for _, options in dog.requests)
    asyncio.run(run())
