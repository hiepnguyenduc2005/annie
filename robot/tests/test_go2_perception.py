"""Fakes-only tests for go2_perception: no SDK, no hardware, no network."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_perception  # noqa: E402

LOWSTATE = {"data": {"bms_state": {"soc": 61, "current": -1200}, "power_v": 28.0,
                     "imu_state": {"rpy": [0.0, 0.0, 0.1]}}}


def pose_msg(x, y, z=0.3, qz=0.0, qw=1.0):
    return {"data": {"header": {"frame_id": "odom"},
                     "pose": {"position": {"x": x, "y": y, "z": z},
                              "orientation": {"x": 0.0, "y": 0.0, "z": qz, "w": qw}}}}


class FakeFrame:
    width, height = 1280, 720

    def __init__(self, n):
        self.n = n


class FakeTrack:
    def __init__(self, frames, period):
        self.frames, self.period = frames, period

    async def recv(self):
        await asyncio.sleep(self.period)
        if not self.frames:
            await asyncio.sleep(3600)
        return self.frames.pop(0)


class FakePubSub:
    def __init__(self, conn):
        self.conn = conn

    def subscribe(self, topic, callback):
        self.conn.subs[topic] = callback


class FakeDataChannel:
    def __init__(self, conn):
        self.conn = conn
        self.pub_sub = FakePubSub(conn)

    async def disableTrafficSaving(self, switch):
        pass

    def switchVideoChannel(self, switch):
        self.conn.video_on = switch


class FakeConn:
    def __init__(self, frames=6, period=0.005, telemetry=True):
        self.subs, self.video_cbs = {}, []
        self.video = type("V", (), {"add_track_callback": lambda s, cb: self.video_cbs.append(cb)})()
        self.datachannel = FakeDataChannel(self)
        self.disconnected = False
        self.video_on = None
        self.frames, self.period, self.telemetry = [FakeFrame(i) for i in range(frames)], period, telemetry
        self.task = None

    async def connect(self):
        self.task = asyncio.ensure_future(self._deliver())

    async def _deliver(self):
        await asyncio.sleep(self.period)
        for cb in self.video_cbs:
            asyncio.ensure_future(cb(FakeTrack(self.frames, self.period)))
        n = 0
        while self.telemetry:
            await asyncio.sleep(self.period)
            n += 1
            self.subs["rt/lf/lowstate"](LOWSTATE)
            self.subs["rt/utlidar/robot_pose"](pose_msg(0.1 * n, 0.0))

    async def disconnect(self):
        self.disconnected = True
        if self.task:
            self.task.cancel()


def lying_track():
    return {"track_id": 4, "box": [30, 80, 200, 160], "conf": 0.8, "posture": "lying",
            "torso_angle_deg": 88.0, "first_seen_ms": 0, "last_seen_ms": 0, "lying_frames": 2}


class FakeTracker:
    def __init__(self, tracks=None):
        self.calls, self.tracks = [], tracks or []

    def update(self, jpeg, *, now_ms):
        self.calls.append((jpeg, now_ms))
        return [dict(t) for t in self.tracks]


def brain_reply(request):
    body = json.loads(request.content)
    return {"perception": {"person": True, "posture": "lying", "location": "floor", "confidence": 0.9,
                           "caption": "a person lying on the floor", "frame_id": body["frame_id"],
                           "ts": body["ts"], "pose": body["pose"], "source": "hardware_vlm", "model": "qwen"},
            "provider": {"mode": "local", "model": "qwen"}, "source": "hardware", "latency_ms": 900.0}


def make_transport(brain_status=200, app_status=200, log=None):
    log = log if log is not None else []

    def handle(request):
        log.append((request.url.host, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/infer":
            return httpx.Response(brain_status, json=brain_reply(request) if brain_status == 200 else {"detail": "vendor secret text"})
        if request.url.path == "/ingest":
            return httpx.Response(app_status, json={"accepted": app_status == 200})
        return httpx.Response(404)
    return httpx.MockTransport(handle), log


FAST = dict(rate_hz=200.0, infer_interval_s=0.02, stale_s=0.5, status_interval_s=0.02, first_telemetry_s=0.5)


def run(conn, tracker, *, transport, duration_s=0.12, **kw):
    return asyncio.run(go2_perception.run_perception(
        ip="10.0.0.99", aes_key=None, map_id="henry-home", brain_url="http://brain", app_url="http://app",
        conn_factory=lambda ip, key: conn, tracker=tracker, encoder=lambda frame: b"jpeg%d" % frame.n,
        http_transport=transport, duration_s=duration_s, **{**FAST, **kw}))


def test_tracks_every_new_frame_and_publishes_perception_with_hardware_identity():
    transport, log = make_transport()
    # Frames arrive slower than the tracker tick, so none is dropped by the latest-frame queue.
    conn, tracker = FakeConn(frames=6, period=0.02), FakeTracker([lying_track()])
    report = run(conn, tracker, transport=transport, duration_s=0.3)
    assert report["frames"] == 6 and len(tracker.calls) == 6
    assert [c[0] for c in tracker.calls][:2] == [b"jpeg0", b"jpeg1"]
    infers = [entry for entry in log if entry[1] == "/infer"]
    assert infers and infers[0][0] == "brain"
    body = infers[0][2]
    assert body["source"] == "hardware" and body["pose"]["map_id"] == "henry-home"
    assert body["jpeg_b64"] and "jpeg_b64" not in json.dumps(report)
    ingests = [entry for entry in log if entry[1] == "/ingest" and entry[2]["channel"] == "brain.perception"]
    assert ingests and ingests[0][2]["data"]["frame_id"] == body["frame_id"]
    assert ingests[0][2]["data"]["source"] == "hardware_vlm"
    assert report["inferences"] >= 1 and report["ingest_accepted"] >= 1
    assert report["max_lying_frames"] == 2 and report["tracks_seen"] == 1
    assert conn.disconnected is True and report["completed"] is True


def test_publishes_dog_status_with_measured_battery_and_pose():
    transport, log = make_transport()
    run(FakeConn(frames=3), FakeTracker(), transport=transport)
    statuses = [e[2]["data"] for e in log if e[1] == "/ingest" and e[2]["channel"] == "dog.status"]
    assert statuses and statuses[0]["battery_pct"] == 61 and statuses[0]["state"] == "idle"
    assert statuses[0]["pose"]["map_id"] == "henry-home" and statuses[0]["waypoint"] is None


def test_inference_is_rate_limited_and_never_concurrent():
    transport, log = make_transport()
    run(FakeConn(frames=40, period=0.002), FakeTracker(), transport=transport, infer_interval_s=0.05, duration_s=0.16)
    infers = [e for e in log if e[1] == "/infer"]
    assert 1 <= len(infers) <= 4


def test_brain_failure_is_sanitized_and_tracking_continues():
    transport, log = make_transport(brain_status=502)
    report = run(FakeConn(frames=5, period=0.02), FakeTracker(), transport=transport, duration_s=0.3)
    assert report["frames"] == 5 and report["inference_errors"] >= 1
    assert "vendor secret text" not in json.dumps(report)
    assert not [e for e in log if e[1] == "/ingest" and e[2]["channel"] == "brain.perception"]


def test_missing_telemetry_publishes_nothing_and_reports():
    transport, log = make_transport()
    report = run(FakeConn(frames=3, telemetry=False), FakeTracker(), transport=transport, first_telemetry_s=0.05)
    assert report["reason"] == "telemetry_missing" and report["completed"] is False
    assert not [e for e in log if e[1] == "/infer"]
    assert not [e for e in log if e[1] == "/ingest"]


def test_no_motion_command_is_ever_sent():
    transport, log = make_transport()
    conn = FakeConn(frames=3)
    sent = []
    conn.datachannel.pub_sub.publish_without_callback = lambda *a, **k: sent.append(a)
    conn.datachannel.pub_sub.publish_request_new = lambda *a, **k: sent.append(a)
    run(conn, FakeTracker(), transport=transport)
    assert sent == []


def test_yaw_from_quaternion():
    assert go2_perception.yaw_from_quaternion({"x": 0, "y": 0, "z": 0, "w": 1}) == pytest.approx(0.0)
    assert go2_perception.yaw_from_quaternion({"x": 0, "y": 0, "z": 0.7071068, "w": 0.7071068}) == pytest.approx(1.5707963, abs=1e-5)


@pytest.mark.parametrize("ip", ["example.com", "8.8.8.8", "::1"])
def test_cli_rejects_non_lan_targets(ip):
    with pytest.raises(SystemExit):
        go2_perception.main(["--ip", ip])
