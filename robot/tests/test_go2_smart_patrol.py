import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_smart_patrol import (PatrolPlanner, StallDetector, body_frame, quaternion_yaw,  # noqa: E402
                              sector_ranges, voxel_points_world)

np = pytest.importorskip("numpy")


def test_quaternion_yaw():
    assert quaternion_yaw({"x": 0, "y": 0, "z": 0, "w": 1}) == pytest.approx(0.0)
    h = math.sqrt(0.5)
    assert quaternion_yaw({"x": 0, "y": 0, "z": h, "w": h}) == pytest.approx(math.pi / 2)


def test_voxel_points_and_body_frame():
    decoded = {"positions": np.array([0, 0, 0, 2, 0, 0, 2, 0, 0], dtype=np.uint8)}  # duplicate voxel collapses
    pts = voxel_points_world(decoded, {"origin": [1.0, 2.0, 0.0], "resolution": 0.5})
    assert pts.shape == (2, 3)
    assert pts[1].tolist() == pytest.approx([1.0 + 2.5 * 0.5, 2.25, 0.25])
    body = body_frame([[1.0, 1.0, 0.3]], (0.0, 0.0), math.pi / 2)  # facing +y: point is ahead-right
    assert body[0].tolist() == pytest.approx([1.0, -1.0, 0.3], abs=1e-6)


def _scene(front=None, left=None, right=None):
    pts = [(x * 0.1, y * 0.1, 0.0) for x in range(-10, 30) for y in range(-15, 15)]  # floor
    if front is not None:
        pts += [(front, y * 0.05, z) for y in range(-6, 7) for z in (0.2, 0.3, 0.4)]
    if left is not None:
        pts += [(x * 0.1, left, z) for x in range(0, 6) for z in (0.2, 0.3)]
    if right is not None:
        pts += [(x * 0.1, -right, z) for x in range(0, 6) for z in (0.2, 0.3)]
    return np.array(pts, dtype=np.float32)


def test_sector_ranges_drop_floor_and_measure_sectors():
    r = sector_ranges(_scene(front=1.0, left=0.6))
    assert r["front"] == pytest.approx(1.0, abs=0.02)
    assert r["left"] == pytest.approx(0.6, abs=0.02)
    assert r["right"] == math.inf
    assert sector_ranges(_scene())["front"] == math.inf  # floor alone is not an obstacle


def test_stall_detector_fires_only_when_commanded_and_stuck():
    d = StallDetector(window_s=2.0, min_progress_m=0.1)
    assert not d.update(now_s=0.0, pose_xy=(0, 0), commanded_vx=0.25)
    assert not d.update(now_s=1.0, pose_xy=(0.01, 0), commanded_vx=0.25)
    assert d.update(now_s=2.1, pose_xy=(0.02, 0), commanded_vx=0.25)
    # moving normally never fires
    for i in range(6):
        assert not d.update(now_s=10 + i * 0.5, pose_xy=(i * 0.15, 0), commanded_vx=0.25)
    # not commanding forward clears the history
    assert not d.update(now_s=20.0, pose_xy=(5, 5), commanded_vx=0.0)
    assert not d.update(now_s=23.0, pose_xy=(5, 5), commanded_vx=0.25)


def test_planner_cruises_when_clear_and_slows_into_obstacle():
    p = PatrolPlanner(cruise_mps=0.25, stop_m=0.5, clear_m=0.9, slow_m=1.2)
    vx, wz, mode = p.step(now_s=0.0, ranges={}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "cruise" and vx == pytest.approx(0.25) and abs(wz) <= 0.6
    vx, _, mode = p.step(now_s=0.1, ranges={"front": 0.85}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "cruise" and 0.08 < vx < 0.25


def test_planner_blocks_and_turns_toward_roomier_side_with_hysteresis():
    p = PatrolPlanner(stop_m=0.5, clear_m=0.9, min_turn_s=0.5, turn_rps=0.6)
    vx, wz, mode = p.step(now_s=0.0, ranges={"front": 0.3, "left": 0.4, "right": 2.0}, pose_xy=(0, 0), yaw=0.0,
                          origin_xy=(0, 0))
    assert (vx, mode) == (0.0, "blocked") and wz == pytest.approx(-0.6)  # right is roomier
    vx, wz, mode = p.step(now_s=0.2, ranges={"front": 0.95}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "blocked" and vx == 0.0  # clear but the minimum turn has not elapsed
    _, _, mode = p.step(now_s=0.7, ranges={"front": 0.7}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "blocked"  # 0.7 is above stop but below clear: keep turning
    vx, _, mode = p.step(now_s=0.8, ranges={"front": 1.5}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "cruise" and vx > 0


def test_planner_backs_off_after_stall_then_turns():
    p = PatrolPlanner(backoff_s=1.0, backoff_mps=0.12)
    vx, wz, mode = p.step(now_s=0.0, ranges={}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0), stalled=True)
    assert (vx, wz, mode) == (-0.12, 0.0, "backoff") and p.collisions == 1
    vx, _, mode = p.step(now_s=0.5, ranges={}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "backoff" and vx < 0
    vx, wz, mode = p.step(now_s=1.1, ranges={}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "blocked" and vx == 0.0 and abs(wz) > 0
    _, _, mode = p.step(now_s=2.0, ranges={}, pose_xy=(0, 0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "cruise"


def test_planner_homes_when_past_leash():
    p = PatrolPlanner(leash_m=2.0, cruise_mps=0.25)
    # 2.5 m east of origin, facing east: must turn around (no forward speed) toward the origin
    vx, wz, mode = p.step(now_s=0.0, ranges={}, pose_xy=(2.5, 0.0), yaw=0.0, origin_xy=(0, 0))
    assert mode == "homing" and vx == 0.0 and abs(wz) > 0
    # facing west (toward origin): drive home straight
    vx, wz, mode = p.step(now_s=0.5, ranges={}, pose_xy=(2.5, 0.0), yaw=math.pi, origin_xy=(0, 0))
    assert mode == "homing" and vx == pytest.approx(0.25) and abs(wz) < 1e-6
    _, _, mode = p.step(now_s=5.0, ranges={}, pose_xy=(0.5, 0.0), yaw=math.pi, origin_xy=(0, 0))
    assert mode == "cruise"
