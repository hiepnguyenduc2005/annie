"""The operator's command-center page and its /telemetry.json: served by the dog process' LiveView and by the replay."""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.dog.planning.missions import MissionBoard  # noqa: E402
from robot.dog.runtime.patrol import COMMAND_CENTER_PATH, LiveView, telemetry_snapshot  # noqa: E402


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def test_page_and_telemetry_routes(tmp_path):
    view = LiveView(port=18011)
    board = MissionBoard()
    view.missions = board
    view.report = {"connection": {"status": "connected"}, "brain": {"enabled": True, "decisions": [{"t_s": 1.0, "action": "explore", "seconds": 5, "reason": "open floor"}]},
                   "voice": {"commands": []}, "greetings": [{"t_s": 2.0, "text": "hello", "identity": {"name": "Jeanine"}}], "checkins": [], "collisions": []}
    view.frontier = {"planner": object(), "goal": (1.25, -0.5)}
    view.map_geometry = {"origin": [0.0, 0.0], "size_m": 12.0, "cell_m": 0.1}
    code, receipt = board.submit({"name": "say", "args": {"text": "hi"}})
    assert code == 202
    url = view.start()
    try:
        status, ctype, body = _get(url)
        assert status == 200 and ctype.startswith("text/html") and b"Annie command center" in body
        assert COMMAND_CENTER_PATH.exists() and b"/telemetry.json" in body
        status, ctype, body = _get(url + "telemetry.json")
        t = json.loads(body)
        assert status == 200 and t["connected"] is True
        assert t["brain"]["decisions"][0]["action"] == "explore"
        assert t["greetings"][0]["name"] == "Jeanine"
        assert t["frontier"] == {"available": True, "goal": [1.25, -0.5]}
        assert t["map"]["size_m"] == 12.0
        assert t["missions"][0]["name"] == "say" and t["missions"][0]["state"] == "accepted"
        status, ctype, body = _get(url + "classic")
        assert status == 200 and b"Annie live" in body
    finally:
        view.server.shutdown()


def test_telemetry_degrades_without_optional_parts():
    view = LiveView(port=0)
    t = telemetry_snapshot(view)
    assert t["connected"] is False and t["missions"] == [] and t["graph_sentences"] == [] and t["map"] is None
    assert t["frontier"] == {"available": False, "goal": None}


def test_mission_board_recent_is_newest_first():
    board = MissionBoard()
    for text in ("one", "two", "three"):
        code, receipt = board.submit({"name": "say", "args": {"text": text}})
        assert code == 202
        board.finish(board.take(), result={"ok": True})
    recent = board.recent(2)
    assert [r["args"]["text"] for r in recent] == ["three", "two"]
    assert all(r["state"] == "completed" for r in recent)
