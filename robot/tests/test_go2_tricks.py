"""Fakes-only tests for go2_tricks: no SDK, no hardware, no network."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_tricks  # noqa: E402

LOWSTATE = {"data": {"bms_state": {"soc": 70, "current": -1200}, "power_v": 28.0,
                     "imu_state": {"rpy": [0.0, 0.0, 0.1]}}}


def pose_msg(x=0.0, y=0.0, z=0.3):
    return {"data": {"header": {"frame_id": "odom"},
                     "pose": {"position": {"x": x, "y": y, "z": z},
                              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}}}


class FakePubSub:
    def __init__(self, conn):
        self.conn = conn

    def subscribe(self, topic, callback):
        self.conn.subs[topic] = callback

    async def publish_request_new(self, topic, options):
        self.conn.requests.append((topic, dict(options)))
        code = self.conn.codes.get(options["api_id"], 0)
        return {"data": {"header": {"status": {"code": code}}}}


class FakeDataChannel:
    def __init__(self, conn):
        self.conn = conn
        self.pub_sub = FakePubSub(conn)

    async def disableTrafficSaving(self, switch):
        pass


class FakeConn:
    def __init__(self, soc=70, codes=None, period=0.005, telemetry=True, tilt=0.0):
        self.subs, self.requests = {}, []
        self.datachannel = FakeDataChannel(self)
        self.codes = codes or {}
        self.soc, self.period, self.telemetry, self.tilt = soc, period, telemetry, tilt
        self.disconnected = False
        self.task = None

    async def connect(self):
        self.task = asyncio.ensure_future(self._deliver())

    async def _deliver(self):
        low = json.loads(json.dumps(LOWSTATE))
        low["data"]["bms_state"]["soc"] = self.soc
        low["data"]["imu_state"]["rpy"] = [self.tilt, 0.0, 0.1]
        while self.telemetry:
            await asyncio.sleep(self.period)
            self.subs["rt/lf/lowstate"](low)
            self.subs["rt/utlidar/robot_pose"](pose_msg())

    async def disconnect(self):
        self.disconnected = True
        if self.task:
            self.task.cancel()


FAST = dict(settle_s=0.01, first_telemetry_s=0.2, stale_s=0.5)


def run(names, conn, **kw):
    return asyncio.run(go2_tricks.run_tricks(list(names), ip="10.0.0.99", aes_key=None,
                                             conn_factory=lambda ip, key: conn, **{**FAST, **kw}))


def api_ids(conn):
    return [o["api_id"] for _, o in conn.requests]


def test_catalog_tiers_and_ids():
    assert go2_tricks.TRICKS["hello"] == (1016, "gentle")
    assert go2_tricks.TRICKS["backflip"][1] == "acrobatic"
    assert {"stand", "sit", "hello", "stretch", "dance1", "dance2", "heart", "stand_down"} <= set(go2_tricks.TRICKS)
    assert all(tier in ("gentle", "dynamic", "acrobatic") for _, tier in go2_tricks.TRICKS.values())


def test_gentle_sequence_stands_first_and_ends_with_stopmove():
    conn = FakeConn()
    report = run(["hello", "stretch"], conn)
    ids = api_ids(conn)
    assert ids[:2] == [1004, 1002]  # StandUp, BalanceStand before any trick
    assert 1016 in ids and 1017 in ids and ids[-1] == 1003
    assert [t["name"] for t in report["performed"]] == ["hello", "stretch"]
    assert all(t["code"] == 0 for t in report["performed"])
    assert report["completed"] is True and conn.disconnected is True


def test_acrobatic_tricks_require_explicit_opt_in():
    conn = FakeConn()
    report = run(["backflip"], conn)
    assert 2043 not in api_ids(conn)
    assert report["reason"] == "acrobatic_not_allowed"
    conn = FakeConn()
    report = run(["backflip"], conn, allow_acrobatic=True)
    assert 2043 in api_ids(conn) and report["performed"][0]["name"] == "backflip"


def test_acrobatic_needs_higher_battery_than_gentle():
    conn = FakeConn(soc=45)
    assert run(["hello"], conn)["completed"] is True
    conn = FakeConn(soc=45)
    report = run(["backflip"], conn, allow_acrobatic=True)
    assert report["reason"] == "battery_low_for_acrobatic" and 2043 not in api_ids(conn)


def test_low_battery_refuses_everything():
    conn = FakeConn(soc=30)
    report = run(["hello"], conn)
    assert api_ids(conn) == [] and report["reason"] == "battery_low"


def test_tilted_robot_refuses_before_standing():
    conn = FakeConn(tilt=0.6)
    report = run(["hello"], conn)
    assert api_ids(conn) == [] and report["reason"] == "robot_not_level"


def test_rejected_trick_code_stops_the_sequence_and_reports():
    conn = FakeConn(codes={1022: 3104})
    report = run(["dance1", "hello"], conn)
    ids = api_ids(conn)
    assert 1022 in ids and 1016 not in ids and ids[-1] == 1003
    assert report["performed"][0] == {"name": "dance1", "api_id": 1022, "code": 3104, "duration_s": pytest.approx(report["performed"][0]["duration_s"])}
    assert report["reason"] == "trick_rejected:dance1:3104" and report["completed"] is False


def test_missing_telemetry_never_commands():
    conn = FakeConn(telemetry=False)
    report = run(["hello"], conn, first_telemetry_s=0.05)
    assert api_ids(conn) == [] and report["reason"] == "telemetry_missing"


def test_unknown_trick_is_rejected_before_connecting():
    with pytest.raises(ValueError):
        go2_tricks.resolve(["moonwalk"])


@pytest.mark.parametrize("ip", ["example.com", "8.8.8.8"])
def test_cli_rejects_non_lan_targets(ip):
    with pytest.raises(SystemExit):
        go2_tricks.main(["--ip", ip, "hello"])
