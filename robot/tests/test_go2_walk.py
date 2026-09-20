"""Fakes-only tests for go2_walk: no SDK, no hardware, no network."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_walk  # noqa: E402

TOPIC_SPORT = "rt/api/sport/request"
LOWSTATE = {"data": {"bms_state": {"soc": 55, "current": -2481},
                     "power_v": 28.3, "imu_state": {"rpy": [0.0, 0.0, 3.0]}}}


def pose_msg(x, y):
    return {"data": {"header": {"frame_id": "odom"},
                     "pose": {"position": {"x": x, "y": y, "z": 0.1},
                              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}}}


class FakePubSub:
    def __init__(self, conn):
        self.conn = conn

    def subscribe(self, topic, callback):
        self.conn.subs[topic] = callback

    async def publish_request_new(self, topic, options):
        self.conn.requests.append((topic, options))
        code = self.conn.codes.get(options["api_id"], 0)
        return {"data": {"header": {"status": {"code": code}}}}

    def publish_without_callback(self, topic, data=None, msg_type=None):
        self.conn.sent.append((topic, data, msg_type))


class FakeDataChannel:
    def __init__(self, conn):
        self.pub_sub = FakePubSub(conn)

    async def disableTrafficSaving(self, switch):
        pass


class FakeConn:
    """Delivers telemetry every `period` seconds; pose advances `step_m` per tick."""

    def __init__(self, soc=55, period=0.005, step_m=0.0, stop_after=None, codes=None,
                 pose_after_stop=None):
        self.subs, self.requests, self.sent = {}, [], []
        self.datachannel = FakeDataChannel(self)
        self.disconnected = False
        self.soc, self.period, self.step_m, self.stop_after = soc, period, step_m, stop_after
        self.codes = codes or {}
        self.pose_after_stop = pose_after_stop
        self.x = 0.0
        self.task = None

    async def connect(self):
        self.task = asyncio.ensure_future(self._deliver())

    async def _deliver(self):
        low = json.loads(json.dumps(LOWSTATE))
        low["data"]["bms_state"]["soc"] = self.soc
        ticks = 0
        while True:
            await asyncio.sleep(self.period)
            ticks += 1
            if self.stop_after is not None and ticks > self.stop_after:
                return  # telemetry goes silent
            if any(r[1]["api_id"] == go2_walk.SPORT_CMD["StopMove"] for r in self.requests):
                if self.pose_after_stop is not None:
                    self.x = self.pose_after_stop
            elif any(s[1]["header"]["identity"]["api_id"] == go2_walk.SPORT_CMD["Move"]
                     for s in self.sent):
                self.x += self.step_m
            if "rt/lf/lowstate" in self.subs:
                self.subs["rt/lf/lowstate"](low)
            if "rt/utlidar/robot_pose" in self.subs:
                self.subs["rt/utlidar/robot_pose"](pose_msg(self.x, 0.0))

    async def disconnect(self):
        self.disconnected = True
        if self.task:
            self.task.cancel()


FAST = dict(tick_s=0.005, stale_s=0.05, settle_s=0.0, first_telemetry_s=0.2, stop_observe_s=0.02)


def run(plan, conn, **kw):
    opts = {**FAST, **kw}
    return asyncio.run(go2_walk.run_walk(plan, ip="10.0.0.99", aes_key=None,
                                         conn_factory=lambda ip, key: conn, **opts))


def api_ids(conn):
    return [o["api_id"] for _, o in conn.requests]


def move_cmds(conn):
    return [s for s in conn.sent if s[1]["header"]["identity"]["api_id"] == go2_walk.SPORT_CMD["Move"]]


# ---- planning ---------------------------------------------------------------

def test_plan_line_derives_duration_from_distance():
    plan = go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0)
    assert plan.shape == "line" and plan.vx_mps == 0.3 and plan.yaw_rps == 0.0
    assert plan.duration_s == pytest.approx(1.0 / 0.3)


def test_plan_circle_derives_yaw_rate_and_lap_duration():
    plan = go2_walk.plan_motion(speed_mps=0.3, circle_radius_m=1.0, laps=2, boundary_radius_m=2.5)
    assert plan.shape == "circle"
    assert plan.yaw_rps == pytest.approx(0.3)
    assert plan.duration_s == pytest.approx(2 * 2 * 3.141592653589793 * 1.0 / 0.3)


def test_plan_duration_override_keeps_walking_for_that_long():
    plan = go2_walk.plan_motion(speed_mps=0.3, circle_radius_m=1.0, duration_s=3600)
    assert plan.duration_s == 3600


@pytest.mark.parametrize("kw", [
    dict(speed_mps=0.0, distance_m=1.0),
    dict(speed_mps=go2_walk.MAX_SPEED_MPS + 0.01, distance_m=1.0),
    dict(speed_mps=0.3, distance_m=-1.0),
    dict(speed_mps=0.3, distance_m=3.0, boundary_radius_m=2.5),          # line leaves boundary
    dict(speed_mps=0.3, circle_radius_m=1.5, boundary_radius_m=2.5),     # 2r exceeds boundary
    dict(speed_mps=0.3, distance_m=1.0, circle_radius_m=1.0),            # ambiguous shape
    dict(speed_mps=0.3),                                                 # no shape
    dict(speed_mps=0.3, distance_m=1.0, duration_s=100, boundary_radius_m=2.5),  # line too long
])
def test_plan_rejects_unsafe_or_ambiguous_requests(kw):
    with pytest.raises(ValueError):
        go2_walk.plan_motion(**kw)


# ---- execution ---------------------------------------------------------------

def test_walk_stands_moves_then_stops_and_disconnects():
    conn = FakeConn(step_m=0.01)
    plan = go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.05)
    report = run(plan, conn)
    ids = api_ids(conn)
    assert ids[:2] == [go2_walk.SPORT_CMD["StandUp"], go2_walk.SPORT_CMD["BalanceStand"]]
    assert ids[-1] == go2_walk.SPORT_CMD["StopMove"]
    moves = move_cmds(conn)
    assert len(moves) >= 3
    assert json.loads(moves[0][1]["parameter"]) == {"x": 0.3, "y": 0.0, "z": 0.0}
    assert moves[0][2] == "req" and moves[0][0] == TOPIC_SPORT
    assert report["reason"] == "duration_complete"
    assert report["completed"] is True
    assert conn.disconnected is True
    assert report["stop"]["ack_ms"] >= 0
    assert report["max_distance_from_origin_m"] > 0


def test_circle_sends_yaw_rate():
    conn = FakeConn()
    plan = go2_walk.plan_motion(speed_mps=0.3, circle_radius_m=1.0, duration_s=0.03)
    run(plan, conn)
    assert json.loads(move_cmds(conn)[0][1]["parameter"])["z"] == pytest.approx(0.3)


@pytest.mark.parametrize("soc", [15, 23, 39.9])
def test_low_battery_never_moves(soc):
    conn = FakeConn(soc=soc)
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.05), conn)
    assert move_cmds(conn) == []
    assert go2_walk.SPORT_CMD["StandUp"] not in api_ids(conn)
    assert report["reason"] == "battery_low"
    assert report["completed"] is False
    assert conn.disconnected is True


@pytest.mark.parametrize("threshold", [40.0, 60.0, 100.0])
def test_battery_at_valid_threshold_allows_stand(threshold):
    conn = FakeConn(soc=threshold)
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.03),
                 conn, min_soc=threshold)
    assert go2_walk.SPORT_CMD["StandUp"] in api_ids(conn)
    assert report["reason"] == "duration_complete"


def test_default_battery_floor_accepts_40_percent():
    conn = FakeConn(soc=40.0)
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.03), conn)
    assert go2_walk.SPORT_CMD["StandUp"] in api_ids(conn)
    assert report["reason"] == "duration_complete"


@pytest.mark.parametrize("threshold", [23, 39.9, 100.1, float("nan"), float("inf"), -float("inf")])
def test_api_rejects_invalid_battery_threshold_before_connection(threshold):
    def forbidden_connection(*args):
        pytest.fail("invalid battery threshold must not create a connection")

    plan = go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0)
    with pytest.raises(ValueError, match="minimum battery"):
        asyncio.run(go2_walk.run_walk(plan, ip="10.0.0.99", aes_key=None,
                                     conn_factory=forbidden_connection, min_soc=threshold))


@pytest.mark.parametrize("threshold", ["23", "39.9", "100.1", "nan", "inf", "-inf"])
def test_cli_rejects_invalid_battery_threshold_before_connection(monkeypatch, threshold):
    def forbidden_connection(*args):
        pytest.fail("invalid battery threshold must not create a connection")

    monkeypatch.setattr(go2_walk, "_default_conn_factory", forbidden_connection)
    with pytest.raises(SystemExit) as exc:
        go2_walk.main(["--distance", "1", f"--min-battery={threshold}"])
    assert exc.value.code == 2


def test_missing_telemetry_never_moves():
    conn = FakeConn(stop_after=0)
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.05), conn)
    assert move_cmds(conn) == []
    assert report["reason"] == "telemetry_missing"


def test_stale_telemetry_mid_walk_stops():
    conn = FakeConn(stop_after=5)
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=5.0), conn)
    assert api_ids(conn)[-1] == go2_walk.SPORT_CMD["StopMove"]
    assert report["reason"] == "telemetry_stale"
    assert report["completed"] is False


def test_leaving_boundary_stops():
    conn = FakeConn(step_m=0.5)  # pose jumps 0.5 m per tick
    plan = go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=5.0, boundary_radius_m=2.5)
    report = run(plan, conn)
    assert api_ids(conn)[-1] == go2_walk.SPORT_CMD["StopMove"]
    assert report["reason"] == "boundary_exceeded"
    assert report["completed"] is False


def test_stand_failure_code_prevents_motion():
    conn = FakeConn(codes={go2_walk.SPORT_CMD["BalanceStand"]: 3104})
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.05), conn)
    assert move_cmds(conn) == []
    assert report["reason"] == "stand_failed:3104"
    assert api_ids(conn)[-1] == go2_walk.SPORT_CMD["StopMove"]


def test_exception_mid_walk_still_stops_and_disconnects():
    class Boom(FakeConn):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def connect(self):
            await super().connect()

    conn = Boom()
    original = conn.datachannel.pub_sub.publish_without_callback

    def explode(topic, data=None, msg_type=None):
        conn.n += 1
        if conn.n == 3:
            raise RuntimeError("private vendor text")
        original(topic, data, msg_type)

    conn.datachannel.pub_sub.publish_without_callback = explode
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=5.0), conn)
    assert api_ids(conn)[-1] == go2_walk.SPORT_CMD["StopMove"]
    assert conn.disconnected is True
    assert report["reason"] == "error:RuntimeError"
    assert "private vendor text" not in json.dumps(report)


def test_cancellation_sends_stop():
    conn = FakeConn()
    plan = go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=5.0)

    async def main():
        task = asyncio.create_task(go2_walk.run_walk(
            plan, ip="10.0.0.99", aes_key=None, conn_factory=lambda ip, key: conn, **FAST))
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    report = asyncio.run(main())
    assert api_ids(conn)[-1] == go2_walk.SPORT_CMD["StopMove"]
    assert report["reason"] == "operator_cancelled"
    assert conn.disconnected is True


def test_connect_failure_is_sanitized():
    class Bad(FakeConn):
        async def connect(self):
            raise RuntimeError("secret sdp material")
    report = run(go2_walk.plan_motion(speed_mps=0.3, distance_m=1.0, duration_s=0.05), Bad())
    assert report["reason"] == "connect_failed:RuntimeError"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("ip", ["example.com", "8.8.8.8", "127.0.0.1", "::1"])
def test_cli_rejects_non_lan_targets(ip):
    with pytest.raises(SystemExit):
        go2_walk.main(["--ip", ip, "--distance", "1"])


def test_cli_speed_cap_enforced():
    with pytest.raises(SystemExit):
        go2_walk.main(["--distance", "1", "--speed", "2.0"])


def test_motion_inhibit_blocks_queued_cli_before_starting_async_hardware(monkeypatch, tmp_path):
    marker = tmp_path / 'motion-inhibit'
    marker.write_text('Another task owns the physical dog.')
    monkeypatch.setattr(go2_walk, 'MOTION_INHIBIT_PATH', marker)
    def forbidden_run(*args, **kwargs):
        pytest.fail('inhibited CLI must not start the hardware coroutine')
    monkeypatch.setattr(go2_walk.asyncio, 'run', forbidden_run)
    assert go2_walk.main(['--distance', '1']) == 2
    assert go2_walk.main(['--circle-radius', '1', '--duration', '60']) == 2
