"""Runtime tests for go2_patrol_greet with a simulated dog: no SDK, no hardware, no network.

The fake robot integrates the commanded forward speed into its pose, publishes
lowstate/pose/voxel telemetry, and can (a) refuse to advance past an unseen
obstacle, which must trip the odometry collision detector, or (b) show a wall
in its LiDAR voxel map, which must make the planner turn before touching it.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go2_patrol_greet  # noqa: E402
from go2_smart_patrol import PatrolPlanner, StallDetector  # noqa: E402

np = pytest.importorskip("numpy")

LOWSTATE = {"data": {"bms_state": {"soc": 55, "current": -2481}, "power_v": 28.3, "imu_state": {"rpy": [0, 0, 3.0]}}}


def pose_msg(x, y):
    return {"data": {"header": {"frame_id": "odom"}, "pose": {"position": {"x": x, "y": y, "z": 0.1},
                                                              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}}}


def voxel_msg(wall_x=None):
    """Voxel map already decoded by the driver: floor at z=0 plus an optional wall across +x."""
    res, origin = 0.05, (-1.0, -1.0, 0.0)
    cells = [(x, y, 0) for x in range(0, 60) for y in range(0, 40)]
    if wall_x is not None:
        wx = int(round((wall_x - origin[0]) / res)) - 1  # cell centre lands on wall_x
        cells += [(wx, y, z) for y in range(0, 40) for z in (4, 6, 8)]
    positions = np.array(cells, dtype=np.uint8).reshape(-1)
    return {"data": {"origin": list(origin), "resolution": res, "data": {"positions": positions}}}


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
        self.conn.requests.append((topic, options))
        return {"data": {"header": {"status": {"code": 0}}}}

    def publish_without_callback(self, topic, data=None, msg_type=None):
        self.conn.sent.append((topic, data, msg_type))
        if topic == go2_patrol_greet.TOPIC_SPORT and isinstance(data, dict):
            self.conn.last_vx = json.loads(data["parameter"])["x"]


class FakeDataChannel:
    def __init__(self, conn):
        self.pub_sub = FakePubSub(conn)

    async def disableTrafficSaving(self, switch):
        pass

    def switchVideoChannel(self, switch):
        pass


class FakeDog:
    """Pose integrates the commanded speed; `block_at_x` is an unseen obstacle, `wall_x` a LiDAR-visible one."""

    def __init__(self, *, period=0.004, step_m=0.02, block_at_x=None, wall_x=None):
        self.subs, self.requests, self.sent = {}, [], []
        self.datachannel, self.video = FakeDataChannel(self), FakeVideo()
        self.period, self.step_m, self.block_at_x, self.wall_x = period, step_m, block_at_x, wall_x
        self.x, self.last_vx, self.max_x, self.task, self.disconnected = 0.0, 0.0, 0.0, None, False

    async def connect(self):
        self.task = asyncio.ensure_future(self._deliver())

    async def _deliver(self):
        while True:
            await asyncio.sleep(self.period)
            if self.last_vx > 0 and (self.block_at_x is None or self.x < self.block_at_x):
                self.x += self.step_m
            elif self.last_vx < 0:
                self.x -= self.step_m
            self.max_x = max(self.max_x, self.x)
            self.subs["rt/lf/lowstate"](LOWSTATE)
            self.subs["rt/utlidar/robot_pose"](pose_msg(self.x, 0.0))
            if go2_patrol_greet.TOPIC_VOXELS in self.subs:
                self.subs[go2_patrol_greet.TOPIC_VOXELS](voxel_msg(self.wall_x))

    async def disconnect(self):
        self.disconnected = True
        if self.task:
            self.task.cancel()


class NobodyTracker:
    def update(self, jpeg, now_ms):
        return []


def run(dog, **kw):
    planner = PatrolPlanner(cruise_mps=0.25, turn_rps=0.5, backoff_s=0.03, min_turn_s=0.02, leash_m=50.0)
    stall = StallDetector(window_s=0.05, min_progress_m=0.02)
    return asyncio.run(go2_patrol_greet.run_patrol_greet(
        ip="10.0.0.99", aes_key=None, conn_factory=lambda ip, key: dog, tracker=NobodyTracker(),
        encoder=lambda frame: (b"", 640, 480), speak=lambda text: None, status=lambda text: None,
        planner=planner, stall=stall, rate_hz=200.0, stale_s=0.2, lidar_stale_s=0.2, boundary_m=100.0, **kw))


def test_unseen_obstacle_trips_collision_backoff_and_turn():
    dog = FakeDog(block_at_x=0.3)
    report = run(dog, duration_s=0.8)
    assert report["reason"] == "duration_complete" and report["completed"]
    assert len(report["collisions"]) >= 1
    assert report["modes"].get("backoff", 0) > 0 and report["modes"].get("blocked", 0) > 0
    reversed_moves = [s for s in dog.sent if s[0] == go2_patrol_greet.TOPIC_SPORT
                      and json.loads(s[1]["parameter"])["x"] < 0]
    assert reversed_moves, "backoff must command a reverse speed"
    assert dog.requests[-1][1]["api_id"] == go2_patrol_greet.STOP_MOVE  # priority stop on exit


def test_lidar_wall_turns_the_dog_before_contact():
    dog = FakeDog(wall_x=1.0, block_at_x=1.0)
    report = run(dog, duration_s=0.6)
    assert report["reason"] == "duration_complete"
    assert report["lidar"]["maps"] > 0
    assert report["lidar"]["first_ranges"]["front"] == pytest.approx(1.0, abs=0.06)
    assert report["modes"].get("blocked", 0) > 0
    assert dog.max_x < 0.6, f"planner must stop before the wall at 1.0 m (reached {dog.max_x:.2f})"
    assert report["collisions"] == []  # seen by LiDAR, so never bumped


def test_open_floor_keeps_cruising_without_collisions():
    dog = FakeDog(wall_x=None)
    report = run(dog, duration_s=0.4)
    assert report["reason"] == "duration_complete"
    assert report["collisions"] == []
    assert set(report["modes"]) == {"cruise"}
    assert dog.max_x > 0.2
