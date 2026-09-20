"""The operator's command-center page and its /telemetry.json: served by the dog process' LiveView and by the replay."""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.dog.planning.missions import MissionBoard  # noqa: E402
from robot.dog.runtime.patrol import COMMAND_CENTER_PATH, LiveView, telemetry_snapshot  # noqa: E402


def test_unsupported_instruction_is_not_reported_as_completed():
    board = MissionBoard()
    code, receipt = board.submit({"name": "instruct", "args": {"text": "do a backflip"}})
    assert code == 202
    parent = board.take()
    board.chain(parent, [], reply="That action is unavailable.", source="rules")
    finished = board.get(receipt["command_id"])
    assert finished["state"] == "failed"
    assert finished["error"] == "That action is unavailable."
    assert board.executing() is None


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


def test_overlapping_missions_wait_their_turn_and_stop_clears_the_line():
    board = MissionBoard()
    c1, r1 = board.submit({"name": "hello"})
    c2, r2 = board.submit({"name": "dance"})
    c3, r3 = board.submit({"name": "say", "args": {"text": "hi"}})
    assert (c1, c2, c3) == (202, 202, 202) and r2["state"] == "queued" and r2["position"] == 1 and r3["position"] == 2
    first = board.take()
    assert first["command_id"] == r1["command_id"] and board.take() is None  # one at a time
    board.finish(first, result={"ok": True})
    second = board.take()
    assert second["command_id"] == r2["command_id"] and second["state"] == "executing"
    assert board.get(r3["command_id"])["state"] == "queued"
    code, stop = board.submit({"name": "stop"})
    assert code == 202 and stop["state"] == "accepted" and board.get(r2["command_id"])["state"] == "cancelled" and board.get(r3["command_id"])["state"] == "cancelled"
    assert board.take() is None
    for i in range(9):
        board.submit({"name": "hello", "command_id": f"h{i}"})
    assert board.submit({"name": "hello", "command_id": "overflow"})[0] == 409  # bounded line


def test_stop_batches_are_correlated_and_new_commands_cannot_overwrite_ack():
    board = MissionBoard(clock=lambda: 12.0)
    for cid in ("stop-1", "stop-2"):
        code, receipt = board.submit({"name": "stop", "command_id": cid})
        assert code == 202 and receipt["state"] == "accepted"
        assert receipt["stop_code"] is None and receipt["processed_at_ms"] is None and receipt["result"] is None
    batch = board.take_stops()
    assert len(batch) == 2 and all(r["state"] == "executing" for r in batch)
    board.submit({"name": "stop", "command_id": "later-stop"})
    board.submit({"name": "say", "command_id": "later-command", "args": {"text": "hello"}})
    board.finish_stops(batch, code=0, ack_ms=3.4)
    for cid in ("stop-1", "stop-2"):
        receipt = board.get(cid)
        assert receipt["state"] == "completed" and receipt["stop_code"] == 0
        assert receipt["processed_at_ms"] == 12000 and receipt["result"]["stop_code"] == 0
    assert board.get("later-stop")["state"] == "accepted"
    assert board.take() is None  # subsequent motion waits for its preceding stop to be processed
    board.finish_stops(board.take_stops(), code=9, ack_ms=1)
    assert board.get("later-stop")["state"] == "failed"
    assert board.take()["command_id"] == "later-command"
    assert board.get("stop-1")["state"] == "completed"


def test_unacknowledged_stop_fails_without_inventing_code_zero():
    board = MissionBoard()
    board.submit({"name": "stop", "command_id": "missing"})
    board.finish_stops(board.take_stops(), code=None, ack_ms=2000, error="StopMove failed: TimeoutError")
    receipt = board.get("missing")
    assert receipt["state"] == "failed" and receipt["stop_code"] is None
    assert receipt["processed_at_ms"] is not None and "TimeoutError" in receipt["error"]


def test_stop_http_returns_correlated_pending_receipt_then_poll_reports_ack():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    view = LiveView(port=port)
    view.missions, view.commands = MissionBoard(), {}
    url = view.start()
    try:
        req = urllib.request.Request(url + "stop", data=b'{"command_id":"http-stop"}',
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            receipt = json.load(response)
            assert response.status == 202
        assert receipt["command_id"] == "http-stop" and receipt["state"] == "accepted"
        assert receipt["stop_code"] is None and receipt["processed_at_ms"] is None
        view.missions.finish_stops(view.missions.take_stops(), code=0, ack_ms=2.1)
        _, _, payload = _get(url + "command/http-stop")
        receipt = json.loads(payload)
        assert receipt["state"] == "completed" and receipt["stop_code"] == 0
        assert receipt["processed_at_ms"] is not None
        assert "not proof" in receipt["result"]["note"]
    finally:
        view.server.shutdown()
        view.server.server_close()


def test_boolean_false_is_not_stop_code_zero():
    board = MissionBoard()
    board.submit({"name": "stop", "command_id": "boolean-code"})
    board.finish_stops(board.take_stops(), code=False, ack_ms=1)
    receipt = board.get("boolean-code")
    assert receipt["state"] == "failed" and receipt["stop_code"] is False
