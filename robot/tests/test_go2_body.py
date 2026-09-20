"""Body service tests with a simulated dog: no SDK, no hardware, no network beyond loopback."""
import asyncio
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_body  # noqa: E402
from go2_body import Body, validate_command  # noqa: E402

LOWSTATE = {"data": {"bms_state": {"soc": 55, "current": -2481}, "power_v": 28.3, "imu_state": {"rpy": [0, 0, 3.0]}}}


def pose_msg(x, y):
    return {"data": {"header": {"frame_id": "odom"}, "pose": {"position": {"x": x, "y": y, "z": 0.1},
                                                              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}}}


class FakeTrack:
    async def recv(self):
        await asyncio.sleep(0.004)
        return object()


class FakeVideo:
    def add_track_callback(self, cb):
        asyncio.ensure_future(cb(FakeTrack()))


class FakePubSub:
    def __init__(self, conn):
        self.conn = conn

    def subscribe(self, topic, callback):
        self.conn.subs[topic] = callback

    async def publish_request_new(self, topic, options):
        self.conn.requests.append(options)
        if options["api_id"] in self.conn.slow:
            await asyncio.sleep(self.conn.slow[options["api_id"]])
        return {"data": {"header": {"status": {"code": self.conn.codes.get(options["api_id"], 0)}}}}

    def publish_without_callback(self, topic, data=None, msg_type=None):
        self.conn.sent.append((topic, data))
        if topic == go2_body.TOPIC_SPORT and isinstance(data, dict):
            self.conn.last_vx = json.loads(data["parameter"])["x"]


class FakeDataChannel:
    def __init__(self, conn):
        self.pub_sub = FakePubSub(conn)

    async def disableTrafficSaving(self, switch):
        pass

    def switchVideoChannel(self, switch):
        pass


class FakeDog:
    def __init__(self, *, soc=55, period=0.004, step_m=0.02, codes=None, slow=None):
        self.subs, self.requests, self.sent = {}, [], []
        self.datachannel, self.video = FakeDataChannel(self), FakeVideo()
        self.soc, self.period, self.step_m = soc, period, step_m
        self.codes, self.slow = codes or {}, slow or {}
        self.x, self.last_vx, self.task, self.disconnected = 0.0, 0.0, None, False

    async def connect(self):
        self.task = asyncio.ensure_future(self._deliver())

    async def _deliver(self):
        low = json.loads(json.dumps(LOWSTATE))
        low["data"]["bms_state"]["soc"] = self.soc
        while True:
            await asyncio.sleep(self.period)
            if self.last_vx > 0:
                self.x += self.step_m
            self.subs["rt/lf/lowstate"](low)
            self.subs["rt/utlidar/robot_pose"](pose_msg(self.x, 0.0))

    async def disconnect(self):
        self.disconnected = True
        if self.task:
            self.task.cancel()


class ScriptedTracker:
    """Returns the given people list on every update; `appear_after` updates of nobody first."""

    def __init__(self, people=None, appear_after=0):
        self.people, self.appear_after, self.updates = people or [], appear_after, 0

    def update(self, jpeg, now_ms):
        self.updates += 1
        return self.people if self.updates > self.appear_after else []


def make_body(dog, tracker=None, **kw):
    return Body(ip="10.0.0.99", aes_key=None, conn_factory=lambda ip, key: dog, tracker=tracker or ScriptedTracker(),
                encoder=lambda frame: (b"jpeg", 640, 480), speaker=lambda text: True,
                listener=lambda max_s: {"transcript": "okay", "heard": True}, status=lambda text: None,
                rate_hz=200.0, stale_s=0.5, **kw)


async def settle(body, timeout=5.0):
    """Wait for the current command to reach a terminal state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while body.current is not None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)


async def connected(dog, **kw):
    body = make_body(dog, **kw)
    assert await body.connect(attempts=1, retry_s=0)
    while body.latest["frame"] is None or not body._telemetry_fresh():
        await asyncio.sleep(0.005)
    return body


# ---- validation -------------------------------------------------------------------------------------------

def test_validate_rejects_unknown_and_out_of_range():
    with pytest.raises(ValueError):
        validate_command({"name": "backflip_into_traffic"})
    with pytest.raises(ValueError):
        validate_command({"name": "move", "args": {"vx": 2.0, "duration_s": 1}})
    with pytest.raises(ValueError):
        validate_command({"name": "say", "args": {"text": ""}})
    with pytest.raises(ValueError):
        validate_command({"name": "hello", "args": {"speed": 3}})
    cid, name, args = validate_command({"name": "find_person", "args": {"name": "Jeanine"}})
    assert name == "find_person" and args == {"name": "Jeanine", "timeout_s": 60.0, "approach": True} and cid


# ---- command lifecycle --------------------------------------------------------------------------------------

def test_trick_stands_first_and_records_codes():
    async def scenario():
        dog = FakeDog()
        body = await connected(dog)
        go2_body.TRICK_SETTLE_S["hello"] = 0.0
        code, receipt = await body.submit({"command_id": "c1", "name": "hello"})
        assert code == 202 and receipt["state"] in ("accepted", "executing")
        await settle(body)
        assert receipt["state"] == "completed"
        assert [r["api_id"] for r in dog.requests[:3]] == [1004, 1002, 1016]
        assert receipt["result"]["codes"]["hello"] == 0
        # same id again is idempotent, not a second trick
        code, again = await body.submit({"command_id": "c1", "name": "hello"})
        assert code == 200 and again is receipt
        await body.close()
    asyncio.run(scenario())


def test_move_streams_and_stops_and_busy_is_refused():
    async def scenario():
        dog = FakeDog()
        body = await connected(dog)
        code, receipt = await body.submit({"command_id": "m1", "name": "move", "args": {"vx": 0.2, "duration_s": 0.2}})
        assert code == 202
        await asyncio.sleep(0.05)
        code, other = await body.submit({"command_id": "m2", "name": "move", "args": {"vx": 0.2, "duration_s": 0.2}})
        assert code == 409 and other["error"] == "busy"
        await settle(body)
        assert receipt["state"] == "completed" and dog.x > 0
        moves = [d for t, d in dog.sent if t == go2_body.TOPIC_SPORT and isinstance(d, dict)]
        assert json.loads(moves[-1]["parameter"])["x"] == 0.0  # final zero velocity
        assert dog.requests[-1]["api_id"] == go2_body.STOP_MOVE and receipt["stop_code"] == 0
        await body.close()
    asyncio.run(scenario())


def test_stop_cancels_the_running_command():
    async def scenario():
        dog = FakeDog()
        body = await connected(dog)
        _, receipt = await body.submit({"command_id": "p1", "name": "patrol", "args": {"duration_s": 5}})
        await asyncio.sleep(0.1)
        code, stop = await body.submit({"command_id": "s1", "name": "stop"})
        assert code == 200 and stop["state"] == "completed" and stop["result"]["stop_code"] == 0
        assert receipt["state"] == "cancelled" and body.current is None
        await body.close()
    asyncio.run(scenario())


def test_battery_floor_refuses_motion_but_not_speech():
    async def scenario():
        dog = FakeDog(soc=30)
        body = await connected(dog)
        _, receipt = await body.submit({"command_id": "w1", "name": "move", "args": {"vx": 0.2, "duration_s": 0.2}})
        await settle(body)
        assert receipt["state"] == "failed" and "floor" in receipt["error"]
        assert not any(r["api_id"] == go2_body.MOVE for r in dog.requests)
        _, say = await body.submit({"command_id": "w2", "name": "say", "args": {"text": "hello"}})
        await settle(body)
        assert say["state"] == "completed" and say["result"]["played"] is True
        await body.close()
    asyncio.run(scenario())


def test_find_person_searches_then_approaches_and_reports_identity():
    person = {"track_id": 7, "box": [300, 100, 340, 220], "posture": "upright", "identity": {"name": "Jeanine", "score": 0.7}}
    tracker = ScriptedTracker([person], appear_after=5)

    async def scenario():
        dog = FakeDog()
        body = await connected(dog, tracker=tracker)
        _, receipt = await body.submit({"command_id": "f1", "name": "find_person",
                                        "args": {"name": "Jeanine", "timeout_s": 3, "approach": True}})
        await settle(body, timeout=6)
        assert receipt["state"] == "completed", receipt
        result = receipt["result"]
        assert result["found"] and result["track_id"] == 7 and result["matched_name"]
        assert result["identity"]["name"] == "Jeanine"
        # the box is small (far) and stays small, so approach keeps walking until the timeout, never "approached"
        assert result["approached"] is False and dog.x > 0
        assert dog.requests[-1]["api_id"] == go2_body.STOP_MOVE
        await body.close()
    asyncio.run(scenario())


def test_find_person_times_out_when_nobody_appears():
    async def scenario():
        dog = FakeDog()
        body = await connected(dog)
        _, receipt = await body.submit({"command_id": "f2", "name": "find_person", "args": {"timeout_s": 1}})
        await settle(body, timeout=4)
        assert receipt["state"] == "completed" and receipt["result"]["found"] is False
        await body.close()
    asyncio.run(scenario())


def test_listen_uses_host_listener():
    async def scenario():
        body = await connected(FakeDog())
        _, receipt = await body.submit({"command_id": "l1", "name": "listen", "args": {"max_s": 2}})
        await settle(body)
        assert receipt["result"] == {"transcript": "okay", "heard": True, "where": "host microphone"}
        await body.close()
    asyncio.run(scenario())


# ---- HTTP layer ----------------------------------------------------------------------------------------------

def _http(method, url, payload=None, token=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Body-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def test_http_surface_commands_status_frame_and_token():
    async def scenario():
        dog = FakeDog()
        body = await connected(dog)
        server = go2_body.serve(body, "127.0.0.1", 0, "secret")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            def call(*a, **k):
                return asyncio.get_running_loop().run_in_executor(None, lambda: _http(*a, **k))
            assert (await call("GET", f"{base}/status"))[0] == 401
            code, ctype, raw = await call("GET", f"{base}/status", token="secret")
            status = json.loads(raw)
            assert code == 200 and status["link"] == "connected" and status["battery_soc_percent"] == 55
            code, ctype, raw = await call("GET", f"{base}/frame.jpg", token="secret")
            assert code == 200 and ctype == "image/jpeg" and raw == b"jpeg"
            code, _, raw = await call("POST", f"{base}/command", {"name": "nope"}, token="secret")
            assert code == 400
            code, _, raw = await call("POST", f"{base}/command", {"command_id": "h1", "name": "say", "args": {"text": "hi"}},
                                      token="secret")
            assert code == 202 and json.loads(raw)["command_id"] == "h1"
            await settle(body)
            code, _, raw = await call("GET", f"{base}/command/h1", token="secret")
            assert code == 200 and json.loads(raw)["state"] == "completed"
            code, _, raw = await call("POST", f"{base}/stop", token="secret")
            assert code == 200 and json.loads(raw)["stop_code"] == 0
        finally:
            server.shutdown()
            await body.close()
    asyncio.run(scenario())
