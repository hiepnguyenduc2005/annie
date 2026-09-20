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
MIN_WALK_MPS = 0.2  # below this the Go2 firmware does not actually walk


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
    # Face vertices repeat voxels; duplicates do not change a minimum range, and np.unique(axis=0)
    # costs ~100 ms per map (measured live), so keep every fourth vertex instead (one per face).
    if positions.size >= 12:
        idx = positions[: positions.size - positions.size % 12].reshape(-1, 4, 3)[:, 0, :]
    else:
        idx = positions[: positions.size - positions.size % 3].reshape(-1, 3)
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


def sector_ranges(points_body, *, ground_z: float | None = None, ground_hint: float | None = None,
                  band=(0.12, 0.60), half_width_m=0.30, side_depth_m=0.90, max_range_m=4.0, min_range_m=0.15,
                  min_points=3) -> dict:
    """Nearest obstacle per sector (metres; inf when clear) from body-frame points.

    Floor voxels are dropped by keeping only the band `ground_z + band[0] .. ground_z + band[1]`.
    When `ground_z` is None it is the 5th percentile of z within 1.5 m, but never below
    `ground_hint` (the floor from the robot's own pose height): stray below-floor voxels
    otherwise drag the estimate down and the real floor becomes an "obstacle" that stops
    the dog in the middle of nowhere. A sector needs at least `min_points` voxels before it
    reports a range, so one noisy voxel cannot stop the dog either. Points closer than
    `min_range_m` are the robot itself and are ignored.
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
        if ground_hint is not None:
            ground_z = max(ground_z, float(ground_hint))
    result["ground_z"] = ground_z
    keep = (pts[:, 2] >= ground_z + band[0]) & (pts[:, 2] <= ground_z + band[1])
    obs = pts[keep]
    dist = np.hypot(obs[:, 0], obs[:, 1])
    obs = obs[(dist >= min_range_m) & (dist <= max_range_m)]
    result["points"] = int(obs.shape[0])
    result["obstacles_body"] = obs
    if obs.shape[0] < min_points:
        return result
    ahead = obs[(obs[:, 0] > 0) & (np.abs(obs[:, 1]) <= half_width_m)]
    if ahead.shape[0] >= min_points:
        result["front"] = float(np.sort(ahead[:, 0])[min_points - 1])  # the nearest *cluster*, not the nearest voxel
    flank = obs[(obs[:, 0] > -0.1) & (obs[:, 0] <= side_depth_m)]
    left = flank[flank[:, 1] > half_width_m]
    right = flank[flank[:, 1] < -half_width_m]
    if left.shape[0] >= min_points:
        result["left"] = float(np.sort(left[:, 1])[min_points - 1])
    if right.shape[0] >= min_points:
        result["right"] = float(np.sort(-right[:, 1])[min_points - 1])
    return result


class StallDetector:
    """Collision by odometry: commanded forward but the pose stopped progressing."""

    def __init__(self, *, window_s=2.0, min_progress_m=0.10, min_cmd_mps=0.18):
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


class OccupancyGrid:
    """Persistent top-down map from the LiDAR voxel maps: obstacle evidence, visited cells, sightings.

    World (odometry) frame, `cell_m` cells over a square `size_m` window centred on the
    patrol origin. `observe` adds body-height obstacle points (evidence decays slowly so a
    moved chair fades); `visit` marks the robot's own footprint as known-free; `free_ahead`
    ray-marches from a pose along a heading to the first known obstacle, which is how the
    heading bandit knows a sector is a wall before the dog walks into it again.
    """

    def __init__(self, origin_xy, *, size_m=12.0, cell_m=0.1, decay=0.995, hit=1.0, threshold=2.0):
        import numpy as np
        self.np = np
        self.origin = (float(origin_xy[0]), float(origin_xy[1]))
        self.cell_m, self.n = cell_m, int(size_m / cell_m)
        self.decay, self.hit, self.threshold = decay, hit, threshold
        self.obstacle = np.zeros((self.n, self.n), dtype=np.float32)
        self.visited = np.zeros((self.n, self.n), dtype=np.uint8)
        self.trail: list[tuple[float, float]] = []
        self.marks: list[tuple[float, float, str]] = []

    def _cell(self, x, y):
        i = int((x - self.origin[0]) / self.cell_m + self.n / 2)
        j = int((y - self.origin[1]) / self.cell_m + self.n / 2)
        return i, j

    def _inside(self, i, j):
        return 0 <= i < self.n and 0 <= j < self.n

    def observe(self, obstacle_points_world):
        """Add obstacle evidence from world-frame points already filtered to the body-height band."""
        np = self.np
        pts = np.asarray(obstacle_points_world, dtype=np.float32).reshape(-1, 3)
        self.obstacle *= self.decay
        if pts.shape[0] == 0:
            return
        ii = ((pts[:, 0] - self.origin[0]) / self.cell_m + self.n / 2).astype(int)
        jj = ((pts[:, 1] - self.origin[1]) / self.cell_m + self.n / 2).astype(int)
        keep = (ii >= 0) & (ii < self.n) & (jj >= 0) & (jj < self.n)
        np.add.at(self.obstacle, (ii[keep], jj[keep]), self.hit)
        np.minimum(self.obstacle, 50.0, out=self.obstacle)

    def visit(self, x, y, radius_m=0.25):
        i0, j0 = self._cell(x, y)
        r = max(1, int(radius_m / self.cell_m))
        for i in range(i0 - r, i0 + r + 1):
            for j in range(j0 - r, j0 + r + 1):
                if self._inside(i, j):
                    self.visited[i, j] = 1
                    self.obstacle[i, j] = 0.0  # the robot is standing here, so it is not a wall
        if not self.trail or math.dist(self.trail[-1], (x, y)) > 0.1:
            self.trail.append((float(x), float(y)))
            self.trail = self.trail[-2000:]

    def mark(self, x, y, kind):
        self.marks.append((float(x), float(y), kind))
        self.marks = self.marks[-200:]

    def free_ahead(self, x, y, heading, max_m=4.0) -> float:
        """Metres along `heading` to the first cell with obstacle evidence above threshold."""
        step = self.cell_m * 0.5
        d = 0.0
        while d < max_m:
            d += step
            i, j = self._cell(x + d * math.cos(heading), y + d * math.sin(heading))
            if not self._inside(i, j):
                return d
            if self.obstacle[i, j] >= self.threshold:
                return d
        return max_m

    def unvisited_ahead(self, x, y, heading, max_m=3.0) -> float:
        """Fraction of cells along the heading (up to the first obstacle) that were never visited."""
        free = min(max_m, self.free_ahead(x, y, heading, max_m))
        n = max(1, int(free / self.cell_m))
        unvisited = 0
        for k in range(1, n + 1):
            i, j = self._cell(x + k * self.cell_m * math.cos(heading), y + k * self.cell_m * math.sin(heading))
            if self._inside(i, j) and not self.visited[i, j]:
                unvisited += 1
        return unvisited / n

    # BGR palettes. "dark": the original debug look. "light": the family app's palette (cream page, ink
    # obstacles, blue-grey trail/marks, one red for the robot) so the command-center page reads as one design.
    PALETTES = {
        "dark": {"unknown": (28, 28, 28), "visited": (60, 60, 60), "occupied": (200, 200, 200), "trail": (200, 120, 0),
                 "greet": (0, 200, 0), "seen": (0, 200, 255), "robot": (0, 0, 255), "heading": (255, 0, 255), "origin": (255, 255, 255)},
        "light": {"unknown": (241, 239, 236), "visited": (255, 255, 255), "occupied": (0, 0, 0), "trail": (100, 90, 69),
                  "greet": (100, 90, 69), "seen": (156, 144, 120), "robot": (46, 58, 163), "heading": (46, 58, 163), "origin": (156, 144, 120)},
        # floor texture for the 3D panel: unknown near-black, walked free space lavender, obstacles dark (blocks sit on top)
        "costmap": {"unknown": (22, 18, 14), "visited": (178, 122, 108), "occupied": (72, 56, 46), "trail": (120, 220, 120),
                    "greet": (120, 220, 120), "seen": (90, 200, 240), "robot": (60, 60, 230), "heading": (230, 90, 230), "origin": (200, 200, 200)},
    }

    def geometry(self) -> dict:
        """World extent of `render()`'s image: centre, side in metres, cell size (for a textured floor plane)."""
        return {"origin": [self.origin[0], self.origin[1]], "size_m": self.n * self.cell_m, "cell_m": self.cell_m}

    def render(self, pose_xy=None, yaw=None, target_heading=None, scale=3, palette="dark"):
        """Top-down JPEG: obstacles, visited cells, trail, marks (greet, seen), robot (with heading) and the explore
        heading, in one of `PALETTES`."""
        import cv2
        np = self.np
        c = self.PALETTES.get(palette, self.PALETTES["dark"])
        img = np.full((self.n, self.n, 3), c["unknown"], dtype=np.uint8)
        img[self.visited.T > 0] = c["visited"]
        occ = (self.obstacle.T >= self.threshold)
        img[occ] = c["occupied"]
        img = cv2.resize(img, (self.n * scale, self.n * scale), interpolation=cv2.INTER_NEAREST)
        img = cv2.flip(img, 0)  # +y up

        def px(x, y):
            i, j = self._cell(x, y)
            return int(i * scale + scale / 2), int(self.n * scale - (j * scale + scale / 2))
        for a, b in zip(self.trail, self.trail[1:]):
            cv2.line(img, px(*a), px(*b), c["trail"], 1, cv2.LINE_AA)
        for x, y, kind in self.marks:
            cv2.circle(img, px(x, y), 4, c["greet"] if kind == "greet" else c["seen"], -1)
        if pose_xy is not None:
            p = px(*pose_xy)
            cv2.circle(img, p, 5, c["robot"], -1)
            if yaw is not None:
                q = px(pose_xy[0] + 0.5 * math.cos(yaw), pose_xy[1] + 0.5 * math.sin(yaw))
                cv2.line(img, p, q, c["robot"], 2, cv2.LINE_AA)
            if target_heading is not None:
                q = px(pose_xy[0] + 0.9 * math.cos(target_heading), pose_xy[1] + 0.9 * math.sin(target_heading))
                cv2.line(img, p, q, c["heading"], 1, cv2.LINE_AA)
        cv2.circle(img, px(*self.origin), 4, c["origin"], 1)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None


class HeadingBandit:
    """UCB1 over heading sectors: explore directions that found people, keep trying the untried ones.

    Arms are `n_arms` world-frame heading sectors. `choose(now)` returns the sector with the
    highest upper confidence bound (untried sectors first), `reward(r)` credits the current arm:
    1.0 when a person turned up that way, 0.0 when the way was blocked. Rewards decay so the
    dog re-explores a sector it wrote off a while ago (people move).
    """

    def __init__(self, n_arms=8, c=0.8, decay=0.985):
        self.n_arms, self.c, self.decay = n_arms, c, decay
        self.pulls = [0.0] * n_arms
        self.value = [0.0] * n_arms
        self.current = None
        self.total = 0.0

    def sector_heading(self, arm) -> float:
        return wrap_angle(2 * math.pi * arm / self.n_arms)

    def arm_for_heading(self, yaw) -> int:
        return int(round((yaw % (2 * math.pi)) / (2 * math.pi) * self.n_arms)) % self.n_arms

    def ucb(self, arm) -> float:
        if self.pulls[arm] < 1e-6:
            return INF
        return self.value[arm] + self.c * math.sqrt(math.log(self.total + 1.0) / self.pulls[arm])

    def choose(self, exclude=None, prior=None) -> int:
        """`prior(arm) -> float` adds map knowledge: negative for known walls, positive for unexplored space."""
        cur = self.current
        def turn_cost(a):  # prefer sectors near the one we are on: less time spent spinning in place
            if cur is None:
                return 0.0
            d = min(abs(a - cur), self.n_arms - abs(a - cur))
            return 0.12 * d
        scores = [(min(self.ucb(a), 3.0) + (prior(a) if prior else 0.0) - turn_cost(a), -abs(a - (cur or 0)), a)
                  for a in range(self.n_arms) if a != exclude]
        best = max(scores)[2]
        self.current = best
        self.pulls[best] += 1.0
        self.total += 1.0
        return best

    def reward(self, r: float, arm=None):
        arm = self.current if arm is None else arm
        if arm is None:
            return
        for a in range(self.n_arms):
            self.value[a] *= self.decay
        n = max(1.0, self.pulls[arm])
        self.value[arm] += (float(r) - self.value[arm]) / n

    def snapshot(self):
        return {"current": self.current, "pulls": [round(p, 1) for p in self.pulls],
                "value": [round(v, 2) for v in self.value]}


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
        self.target_heading = None  # world yaw the cruise steers toward (bandit / brain); None = free sweep
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

        # cruise: steer toward the target heading when there is one, else sweep gently; avoid a close flank
        if self.target_heading is not None:
            err = wrap_angle(self.target_heading - yaw)
            wz = max(-self.turn_rps, min(self.turn_rps, self.k_heading * err))
            if abs(err) > math.pi / 3:  # far off: turn in place first (creeping is below the walking deadband)
                return 0.0, math.copysign(self.turn_rps, err), "cruise"
        else:
            wz = 0.35 * self.turn_rps * math.sin(2.0 * math.pi * (now_s - self.t0) / self.sweep_period_s)
        if left < self.side_m:
            wz -= 0.5 * self.turn_rps
        if right < self.side_m:
            wz += 0.5 * self.turn_rps
        return self._slow(self.cruise_mps, front), max(-self.turn_rps, min(self.turn_rps, wz)), "cruise"

    def _slow(self, vx, front):
        """Slow into the front range, but never below the walking deadband (the Go2 shuffles in place under ~0.15 m/s)."""
        if front >= self.slow_m:
            return vx
        frac = max(0.0, (front - self.stop_m) / max(1e-6, self.slow_m - self.stop_m))
        return max(MIN_WALK_MPS, vx * max(0.35, frac)) if vx > 0 else vx
