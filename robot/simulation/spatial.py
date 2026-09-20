"""Measured MuJoCo range scans and robot trajectory for operator visualization.

The batched raycasting approach follows DimOS, Copyright 2025 Dimensional Inc.,
Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0):
https://github.com/dimensionalOS/dimos/blob/c1c3cdc9d2ee54ca72259465688395699d7d99a2/dimos/simulation/engines/mujoco_engine.py
``_raycast_lidars`` (lines 485–539). Adapted elements: batched mj_multiRay
call, world-direction transform, min/max distance validity mask, and
origin + direction * distance reconstruction. Changes: a panoramic scan and
excludes the entire robot subtree on a private model, rather than excluding just
one body. It requires neither the DimOS runtime nor rendered camera images.
"""
from collections import deque
import copy
import math
import time

import mujoco
import numpy as np


class SpatialSensor:
    """Use on the physics/render thread, after mj_forward/mj_step.

    Construct a new instance when changing models. Call reset on explicit scene
    resets (including resets at time zero); backwards simulation time also resets.
    ``now`` is an optional monotonic wall timestamp for deterministic tests.
    """

    def __init__(self, model, robot_body_id=None, *, hz=5.0, max_range=12.0,
                 history_size=600):
        if not 0 < hz <= 5 or not 0 < max_range <= 50 or not 1 <= history_size <= 2000:
            raise ValueError('require 0 < hz <= 5, 0 < max_range <= 50, 1 <= history_size <= 2000')
        if robot_body_id is None:
            free = np.flatnonzero(model.jnt_type == mujoco.mjtJoint.mjJNT_FREE)
            if not len(free):
                raise ValueError('a robot body or free joint is required')
            robot_body_id = int(model.jnt_bodyid[free[0]])
        if not 0 < robot_body_id < model.nbody:
            raise ValueError('robot body must be a non-world body')
        self.body_id = int(robot_body_id)
        # Only this private copy's ray-selection groups change. Joint state and
        # all geometry transforms remain those of the live data; no mj_forward
        # or stepping is performed with the copy.
        self._ray_model = copy.copy(model)
        robot_bodies = {self.body_id}
        for body in range(self.body_id + 1, model.nbody):
            if int(model.body_parentid[body]) in robot_bodies:
                robot_bodies.add(body)
        self._ray_model.geom_group[:] = 0
        self._ray_model.geom_group[np.isin(model.geom_bodyid, list(robot_bodies))] = 5
        self._groups = np.array([1, 0, 0, 0, 0, 0], dtype=np.uint8)
        azimuth = np.linspace(0, 2 * np.pi, 144, endpoint=False)
        elevation = np.deg2rad([-20, -10, 0, 10, 20])
        a, e = np.meshgrid(azimuth, elevation)
        self._directions = np.column_stack((
            (np.cos(e) * np.cos(a)).ravel(),
            (np.cos(e) * np.sin(a)).ravel(), np.sin(e).ravel()))
        self.interval = 1 / hz
        self.max_range = float(max_range)
        self._trajectory = deque(maxlen=int(history_size))
        self.reset()

    def reset(self):
        self._trajectory.clear()
        self._last_wall = -math.inf
        self._last_sim = -math.inf
        self._frame = None

    def update(self, data, now=None):
        """Capture a scan when due; return whether a new scan was captured."""
        now = time.monotonic() if now is None else float(now)
        sim_time = float(data.time)
        if not math.isfinite(now) or not math.isfinite(sim_time):
            raise ValueError('sensor timestamps must be finite')
        if sim_time < self._last_sim or now < self._last_wall:
            self.reset()
        self._last_sim = sim_time
        if now - self._last_wall < self.interval - 1e-9:
            return False
        position = data.xpos[self.body_id].copy()
        rotation = data.xmat[self.body_id].reshape(3, 3).copy()
        if not np.isfinite(position).all() or not np.isfinite(rotation).all():
            raise ValueError('robot pose must be finite')
        # Explicit virtual sensor mount, 18 cm above measured trunk origin.
        origin = position + rotation @ np.array([0.0, 0.0, 0.18])
        directions = self._directions @ rotation.T
        count = len(directions)
        geom_ids = np.full(count, -1, dtype=np.int32)
        distances = np.full(count, -1.0, dtype=np.float64)
        mujoco.mj_multiRay(self._ray_model, data, origin, directions.ravel(),
                          self._groups, True, -1, geom_ids, distances, None,
                          count, self.max_range)
        valid = (distances >= 0.05) & (distances <= self.max_range) & (geom_ids >= 0)
        points = origin + directions[valid] * distances[valid, None]
        if not self._trajectory or np.linalg.norm(position - self._trajectory[-1]) >= 0.025:
            self._trajectory.append(position.copy())
        self._frame = {
            'source': 'mujoco_raycast_simulation', 'frame': 'world',
            'timestamp': now, 'timestamp_clock': 'monotonic_seconds',
            'simulation_time': sim_time,
            'pose': {'position_m': position.tolist(),
                     'quaternion_wxyz': data.xquat[self.body_id].tolist()},
            'origin_world': origin.tolist(), 'ray_count': count,
            'hit_count': int(valid.sum()), 'min_range_m': 0.05,
            'max_range_m': self.max_range, 'scan_hz_limit': 1 / self.interval,
            'ranges_m': [float(d) if ok else None for d, ok in zip(distances, valid)],
            'points_world': points.tolist(),
            'ray_indices': np.flatnonzero(valid).tolist(),
            'hit_geom_ids': geom_ids[valid].tolist(),
            'excluded_robot_body_id': self.body_id,
            'trajectory_source': 'measured_mujoco_robot_body_position',
        }
        self._last_wall = now
        return True

    def snapshot(self):
        """Return an independent JSON-friendly frame, or None before capture."""
        if self._frame is None:
            return None
        frame = copy.deepcopy(self._frame)
        frame['trajectory_world'] = [point.tolist() for point in self._trajectory]
        return frame

    def draw(self, scene, *, lidar=True, trajectory=True, route=()):
        """Append bounded operator-only geoms after renderer.update_scene.

        Never call on the robot observation renderer. Scene capacity is honored;
        measured path segments receive priority over scan points.
        """
        if self._frame is None:
            return 0
        start = scene.ngeom
        paths = []
        if trajectory:
            paths.append(([np.array([p[0], p[1], 0.06]) for p in self._trajectory],
                          [1.0, 0.65, 0.12, 1.0]))
        if route is not None:
            route_points = []
            for point in route:
                if len(route_points) >= 256:
                    break
                xy = np.asarray(point, dtype=float)[:2]
                if xy.shape == (2,) and np.isfinite(xy).all():
                    route_points.append(np.array([xy[0], xy[1], 0.05]))
            paths.append((route_points, [0.2, 1.0, 0.35, 0.9]))
        for points, color in paths:
            for a, b in zip(points, points[1:]):
                if np.linalg.norm(b - a) < 1e-9:
                    continue
                if scene.ngeom >= scene.maxgeom:
                    break
                geom = scene.geoms[scene.ngeom]
                mujoco.mjv_initGeom(geom, mujoco.mjtGeom.mjGEOM_CAPSULE,
                                   np.zeros(3), np.zeros(3), np.eye(3).ravel(),
                                   np.array(color))
                mujoco.mjv_connector(geom, mujoco.mjtGeom.mjGEOM_CAPSULE, 0.028, a, b)
                geom.emission = 0.8
                scene.ngeom += 1
        if lidar:
            for point in self._frame['points_world']:
                if scene.ngeom >= scene.maxgeom:
                    break
                mujoco.mjv_initGeom(scene.geoms[scene.ngeom], mujoco.mjtGeom.mjGEOM_SPHERE,
                                   np.full(3, 0.04), np.array(point), np.eye(3).ravel(),
                                   np.array([0.05, 0.9, 1.0, 1.0]))
                scene.geoms[scene.ngeom].emission = 0.8
                scene.ngeom += 1
        return scene.ngeom - start
