"""Smart patrol for the physical Go2: reactive wander with collision detection. Pure logic.

Three collision sources, combined by `PatrolPlanner`:
  1. LiDAR sector ranges (front / left / right, metres) from the robot's own
     `rt/utlidar/voxel_map_compressed` occupancy voxels (`voxel_points_world`,
     `sector_ranges`). The floor is removed by estimating the ground height from
     the voxels themselves, so the body-height band is self-calibrating.
  2. Odometry stall (`StallDetector`): the planner commanded forward speed but
     the pose did not progress, i.e. the dog is pushing against something the
     LiDAR did not see (glass, a leg, a low chair rail).
  3. Optional firmware obstacle avoidance (`rt/api/obstacles_avoid`, switched
     on by the runtime when the robot supports it); the planner still runs its
     own sectors on top so a refused switch degrades gracefully.

Planner modes: `cruise` (gentle sweeping wander, slows as the front range
closes), `blocked` (turn in place toward the roomier side until the front
clears, with hysteresis), `backoff` (reverse briefly after a stall, then
turn), `homing` (leash back toward the patrol origin). No hardware here; the
runtime in `go2_patrol_greet.py` feeds it telemetry and sends velocities.
"""
from __future__ import annotations

import math
from collections import deque

INF = float("inf")


def quaternion_yaw(q: dict) -> float:
    """Yaw (rad) about +z from an xyzw quaternion dict."""
    x, y, z, w = (float(q[k]) for k in "xyzw")
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def voxel_points_world(decoded: dict, meta: dict):
    """Occupied voxel centres (N,3) in the odometry frame from a decoded voxel map."""
    import numpy as np
    positions = np.asarray(decoded.get("positions"), dtype=np.uint8)
    if positions.size < 3:
        return np.zeros((0, 3), dtype=np.float32)
    idx = np.unique(positions[: positions.size - positions.size % 3].reshape(-1, 3), axis=0)
    origin = np.asarray(meta.get("origin", (0.0, 0.0, 0.0)), dtype=np.float32)
    resolution = float(meta.get("resolution", 0.05))
    return origin + (idx.astype(np.float32) + 0.5) * resolution


def body_frame(points_world, pose_xy, yaw: float):
    """Rotate/translate world points into the robot frame: x forward, y left, z unchanged."""
    import numpy as np
    pts = np.asarray(points_world, dtype=np.float32)
    if pts.size == 0:
        return pts.reshape(0, 3)
    dx, dy = pts[:, 0] - float(pose_xy[0]), pts[:, 1] - float(pose_xy[1])
    c, s = math.cos(yaw), math.sin(yaw)
    out = np.empty_like(pts)
    out[:, 0] = c * dx + s * dy
    out[:, 1] = -s * dx + c * dy
    out[:, 2] = pts[:, 2]
    return out


def sector_ranges(points_body, *, ground_z: float | None = None, band=(0.12, 0.60), half_width_m=0.30,
                  side_depth_m=0.90, max_range_m=4.0, min_range_m=0.15) -> dict:
    """Nearest obstacle per sector (metres; inf when clear) from body-frame points.

    Floor voxels are dropped by keeping only the band `ground_z + band[0] .. ground_z + band[1]`;
    when `ground_z` is None it is estimated as the 5th percentile of z within 1.5 m.
    Points closer than `min_range_m` are the robot itself and are ignored.
    """
    import numpy as np
    pts = np.asarray(points_body, dtype=np.float32).reshape(-1, 3)
    result = {"front": INF, "left": INF, "right": INF, "ground_z": ground_z, "points": 0}
    if pts.shape[0] == 0:
        return result
    near = pts[np.hypot(pts[:, 0], pts[:, 1]) <= 1.5]
    if ground_z is None:
        if near.shape[0] == 0:
            return result
        ground_z = float(np.percentile(near[:, 2], 5))
    result["ground_z"] = ground_z
    keep = (pts[:, 2] >= ground_z + band[0]) & (pts[:, 2] <= ground_z + band[1])
    obs = pts[keep]
    dist = np.hypot(obs[:, 0], obs[:, 1])
    obs = obs[(dist >= min_range_m) & (dist <= max_range_m)]
    result["points"] = int(obs.shape[0])
    if obs.shape[0] == 0:
        return result
    ahead = obs[(obs[:, 0] > 0) & (np.abs(obs[:, 1]) <= half_width_m)]
    if ahead.shape[0]:
        result["front"] = float(ahead[:, 0].min())
    flank = obs[(obs[:, 0] > -0.1) & (obs[:, 0] <= side_depth_m)]
    left = flank[flank[:, 1] > half_width_m]
    right = flank[flank[:, 1] < -half_width_m]
    if left.shape[0]:
        result["left"] = float(left[:, 1].min())
    if right.shape[0]:
        result["right"] = float((-right[:, 1]).min())
    return result


class StallDetector:
    """Collision by odometry: commanded forward but the pose stopped progressing."""

    def __init__(self, *, window_s=2.0, min_progress_m=0.10, min_cmd_mps=0.08):
        self.window_s = window_s
        self.min_progress_m = min_progress_m
        self.min_cmd_mps = min_cmd_mps
        self.samples: deque = deque()

    def update(self, *, now_s: float, pose_xy, commanded_vx: float) -> bool:
        if commanded_vx < self.min_cmd_mps:
            self.samples.clear()
            return False
        self.samples.append((now_s, (float(pose_xy[0]), float(pose_xy[1]))))
        while self.samples and now_s - self.samples[0][0] > self.window_s * 2:
            self.samples.popleft()
        t0, p0 = self.samples[0]
        if now_s - t0 < self.window_s:
            return False
        if math.dist(p0, self.samples[-1][1]) < self.min_progress_m:
            self.samples.clear()
            return True
        return False


class PatrolPlanner:
    """Deterministic wander + avoidance + leash. `step` returns (vx, wz, mode)."""

    def __init__(self, *, cruise_mps=0.25, turn_rps=0.6, stop_m=0.55, clear_m=0.95, slow_m=1.2,
                 side_m=0.45, leash_m=2.0, sweep_period_s=14.0, backoff_s=1.2, backoff_mps=0.12,
                 min_turn_s=0.6, k_heading=1.2):
        self.cruise_mps, self.turn_rps = cruise_mps, turn_rps
        self.stop_m, self.clear_m, self.slow_m, self.side_m = stop_m, clear_m, slow_m, side_m
        self.leash_m, self.sweep_period_s = leash_m, sweep_period_s
        self.backoff_s, self.backoff_mps, self.min_turn_s = backoff_s, backoff_mps, min_turn_s
        self.k_heading = k_heading
        self.mode = "cruise"
        self.turn_sign = 1.0
        self.mode_since = None
        self.collisions = 0
        self.t0 = None

    @staticmethod
    def _r(ranges, key):
        v = (ranges or {}).get(key)
        return INF if v is None else float(v)

    def _enter(self, mode, now_s, ranges=None):
        if mode in ("blocked", "backoff"):
            left, right = self._r(ranges, "left"), self._r(ranges, "right")
            if left != right:
                self.turn_sign = 1.0 if left > right else -1.0
            else:
                self.turn_sign = -self.turn_sign  # alternate when there is nothing to choose
        self.mode, self.mode_since = mode, now_s

    def step(self, *, now_s, ranges, pose_xy, yaw, origin_xy, stalled=False):
        if self.t0 is None:
            self.t0 = now_s
        if self.mode_since is None:
            self.mode_since = now_s
        front, left, right = (self._r(ranges, k) for k in ("front", "left", "right"))
        held = now_s - self.mode_since
        dist_home = math.dist(pose_xy, origin_xy)

        if stalled and self.mode != "backoff":
            self.collisions += 1
            self._enter("backoff", now_s, ranges)
            held = 0.0
        if self.mode == "backoff":
            if held < self.backoff_s:
                return -self.backoff_mps, 0.0, "backoff"
            self._enter("blocked", now_s, ranges)
            self.turn_sign = self.turn_sign  # keep the side picked at the collision
            held = 0.0
        if self.mode != "blocked" and front < self.stop_m:
            self._enter("blocked", now_s, ranges)
            held = 0.0
        if self.mode == "blocked":
            if front >= self.clear_m and held >= self.min_turn_s:
                self._enter("cruise", now_s)
            else:
                return 0.0, self.turn_sign * self.turn_rps, "blocked"

        if self.mode != "homing" and dist_home > self.leash_m:
            self._enter("homing", now_s)
        if self.mode == "homing":
            if dist_home < self.leash_m * 0.6:
                self._enter("cruise", now_s)
            else:
                err = wrap_angle(math.atan2(origin_xy[1] - pose_xy[1], origin_xy[0] - pose_xy[0]) - yaw)
                wz = max(-self.turn_rps, min(self.turn_rps, self.k_heading * err))
                vx = self.cruise_mps * max(0.0, math.cos(err)) if abs(err) < math.pi / 2 else 0.0
                return self._slow(vx, front), wz, "homing"

        # cruise: sweep gently, steer away from a close flank, slow into the front range
        wz = 0.35 * self.turn_rps * math.sin(2.0 * math.pi * (now_s - self.t0) / self.sweep_period_s)
        if left < self.side_m:
            wz -= 0.5 * self.turn_rps
        if right < self.side_m:
            wz += 0.5 * self.turn_rps
        return self._slow(self.cruise_mps, front), max(-self.turn_rps, min(self.turn_rps, wz)), "cruise"

    def _slow(self, vx, front):
        if front >= self.slow_m:
            return vx
        frac = max(0.0, (front - self.stop_m) / max(1e-6, self.slow_m - self.stop_m))
        return vx * max(0.35, frac)
