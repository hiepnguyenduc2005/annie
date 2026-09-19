"""Fakes-only tests for go2_probe: no SDK, no hardware, no network."""
import asyncio
import json
import copy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_probe  # noqa: E402


LOWSTATE_MSG = {"data": {"bms_state": {"soc": 55, "current": -2481},
                         "power_v": 28.3, "imu_state": {"rpy": [0.0, 0.0, 3.0]}}}
POSE_MSG = {"data": {"header": {"frame_id": "odom"},
                     "pose": {"position": {"x": 1.0, "y": 2.0, "z": 0.1},
                              "orientation": {"x": 0.0, "y": 0.0,
                                              "z": -0.97, "w": -0.24}}}}


class FakeFrame:
    width, height = 1280, 833

    class format:
        name = "yuv420p"


class FakeTrack:
    async def recv(self):
        return FakeFrame()


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
        self.conn.traffic = switch

    def switchVideoChannel(self, switch):
        self.conn.video_on = switch


class FakeConn:
    def __init__(self, deliver=True, malformed=False, delivery_delay=0.01):
        self.deliver = deliver
        self.malformed = malformed
        self.delivery_delay = delivery_delay
        self.subs, self.video_cbs = {}, []
        self.video = type("V", (), {"add_track_callback": lambda self, cb: self.cbs.append(cb)})()
        self.video.cbs = self.video_cbs
        self.datachannel = FakeDataChannel(self)
        self.disconnected = False

    async def connect(self):
        if not self.deliver:
            return
        # Realistic ordering: delivers fire after connect() returns and
        # run_probe() has registered its subscriptions.
        asyncio.ensure_future(self._deliver())

    async def _deliver(self):
        await asyncio.sleep(self.delivery_delay)
        low = {"data": None} if self.malformed else LOWSTATE_MSG
        self.subs[go2_probe.TOPICS["lowstate"]](low)
        self.subs[go2_probe.TOPICS["pose"]](POSE_MSG if not self.malformed else {"data": 3})
        for cb in self.video_cbs:
            await cb(FakeTrack())

    async def disconnect(self):
        self.disconnected = True


def run(ip="10.0.0.99", timeout=1.0, **kw):
    return asyncio.run(go2_probe.run_probe(ip, timeout, **kw))


def test_success_reports_all_streams_and_disconnects():
    report = run(conn_factory=lambda ip, key: FakeConn())
    assert report["completed"] and report["connection"]["status"] == "connected"
    assert report["streams"]["video"]["frame"]["width"] == 1280
    assert report["streams"]["lowstate"]["battery"]["soc_percent"] == 55
    assert report["streams"]["pose"]["position"] == {"x": 1.0, "y": 2.0, "z": 0.1}
    assert report["streams"]["pose"]["orientation_xyzw"]["w"] == -0.24
    assert report["streams"]["pose"]["frame_id"] == "odom"
    assert "host_receipt_unix_s" in report["streams"]["video"]["frame"]


def test_delivery_after_subscription_realistic_ordering():
    # connect() finishing before subscribe() must still deliver: the fake
    # defers its sends until after run_probe registers subscriptions.
    report = run(conn_factory=lambda ip, key: FakeConn(delivery_delay=0.05))
    assert report["completed"]


def test_timeout_marks_streams_missing():
    report = run(conn_factory=lambda ip, key: FakeConn(deliver=False))
    assert not report["completed"]
    assert all(not s["received"] for s in report["streams"].values())
    assert report["connection"]["disconnected"] is True


def test_malformed_telemetry_no_crash_no_success():
    report = run(conn_factory=lambda ip, key: FakeConn(malformed=True))
    assert not report["completed"]
    assert not report["streams"]["lowstate"]["received"]
    assert any("parse_error" in n for n in report["notes"])


def test_connect_error_sanitized_and_nonzero():
    class Boom(FakeConn):
        async def connect(self):
            raise RuntimeError("arbitrary private password is do-not-print-this")
    report = run(conn_factory=lambda ip, key: Boom(deliver=False))
    assert report["connection"]["status"] == "connect_failed"
    assert "do-not-print-this" not in json.dumps(report)
    assert report["connection"]["error"]["type"] == "RuntimeError"


@pytest.mark.parametrize("value", [None, True, "55", float("inf"), -1, 101])
def test_invalid_battery_never_counts_as_telemetry(value):
    message = copy.deepcopy(LOWSTATE_MSG)
    message["data"]["bms_state"]["soc"] = value
    with pytest.raises(ValueError):
        go2_probe.extract_lowstate(message)


def test_child_without_key_still_probes_and_prints_json(capsys, monkeypatch):
    # AES key is optional (older firmware); absence must not be a config error.
    monkeypatch.delenv("UNITREE_AES_128_KEY", raising=False)
    def factory(ip, key):
        assert key is None
        print("VENDOR PRIVATE OUTPUT")
        return FakeConn()
    monkeypatch.setattr(go2_probe, "_default_conn_factory", factory)
    code = go2_probe.main(["--ip", "10.0.0.99", "--timeout", "5", "--_child"])
    captured = capsys.readouterr()
    assert code == 0
    assert "VENDOR PRIVATE OUTPUT" not in captured.out
    report = json.loads(captured.out.strip().splitlines()[-1])
    assert report["connection"]["status"] != "config_error"
    assert report["completed"] is True


def test_parent_writes_output_file_and_stdout_json(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITREE_AES_128_KEY", raising=False)
    def child(*args, **kwargs):
        assert kwargs["env"]["NO_PROXY"] == "*"
        assert not any(k.lower() == "http_proxy" for k in kwargs["env"])
        return SimpleNamespace(returncode=1, stdout=json.dumps(go2_probe.new_report("10.0.0.99")), stderr="")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setattr(go2_probe.subprocess, "run", child)
    out = tmp_path / "report.json"
    code = go2_probe.main(["--ip", "10.0.0.99", "--timeout", "5",
                           "--output", str(out)])
    assert code != 0
    report = json.loads(out.read_text())
    assert report["source"] == "hardware"
    assert report["completed"] is False
    assert "UNITREE" not in out.read_text()


def test_early_video_track_before_connect_returns_is_received():
    class Early(FakeConn):
        async def connect(self):
            for callback in self.video_cbs:
                await callback(FakeTrack())
            asyncio.create_task(self._deliver())
    assert run(conn_factory=lambda ip, key: Early())["completed"]


def test_disconnect_failure_prevents_completed_claim():
    class BrokenDisconnect(FakeConn):
        async def disconnect(self):
            raise RuntimeError("private SDK response")
    report = run(conn_factory=lambda ip, key: BrokenDisconnect())
    assert not report["completed"]
    assert report["connection"]["disconnected"] is False
    assert "private SDK response" not in json.dumps(report)


def test_hard_timeout_produces_failure_without_forwarding_child_output(monkeypatch, capsys):
    def hung(*args, **kwargs):
        raise subprocess.TimeoutExpired("child", 10, output="secret", stderr="secret")
    monkeypatch.setattr(go2_probe.subprocess, "run", hung)
    assert go2_probe.main(["--ip", "10.0.0.99", "--timeout", "5"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["connection"]["status"] == "timeout_hard"


@pytest.mark.parametrize("ip", ["example.com", "8.8.8.8", "127.0.0.1", "0.0.0.0", "::1"])
def test_rejects_non_lan_targets(ip):
    with pytest.raises(SystemExit):
        go2_probe.main(["--ip", ip])
