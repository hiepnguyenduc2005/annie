"""Annie's dimOS skills against a fake body over loopback HTTP (dimOS venv only)."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytest.importorskip("dimos")
from robot.dimos_skills import BodyLink, annie_skills  # noqa: E402


class FakeBody(BaseHTTPRequestHandler):
    receipts = {}
    log = []

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/stop":
            FakeBody.log.append(("stop", None))
            return self._send(200, {"stop_code": 0})
        payload = json.loads(raw)
        FakeBody.log.append((payload.get("name") or payload.get("action"), payload.get("args")))
        if "action" in payload:
            return self._send(202, {"accepted": payload["action"]})
        result = {"find_person": {"found": True, "identity": {"name": "Jeanine"}, "approached": True},
                  "say": {"played": True}, "listen": {"heard": True, "transcript": "okay"},
                  "hello": {"codes": {"hello": 0}}}.get(payload["name"], {})
        receipt = {"command_id": payload["command_id"], "name": payload["name"], "state": "completed", "result": result}
        FakeBody.receipts[payload["command_id"]] = receipt
        self._send(202, {**receipt, "state": "executing"})

    def do_GET(self):
        cid = self.path.rsplit("/", 1)[-1]
        self._send(200, FakeBody.receipts[cid])


@pytest.fixture(scope="module")
def body_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBody)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_library_exposes_tools_and_runs_skills_through_the_body_contract(body_url):
    lib = annie_skills(BodyLink(body_url, poll_s=0.01))
    names = {t["function"]["name"] for t in lib.get_tools()}
    assert {"FindPerson", "Say", "Listen", "Wave", "Stop", "GoHome", "Explore"} <= names
    assert "found" in lib.call("FindPerson", person="Jeanine", timeout_s=5)
    assert "played" in lib.call("Say", text="Hi Jeanine!")
    assert lib.call("Listen", max_s=3) == "heard: 'okay'"
    assert "hello" in lib.call("Wave")
    assert lib.call("GoHome") == "going home"
    assert "stop sent" in lib.call("Stop")
    assert [n for n, _ in FakeBody.log] == ["find_person", "say", "listen", "hello", "go_home", "stop"]
    assert FakeBody.log[0][1] == {"name": "Jeanine", "timeout_s": 5.0, "approach": True}
