"""dimOS frontier exploration driven by Annie's own occupancy grid. Offline: no ROS, LCM or Zenoh.

What is dimOS and what is ours
------------------------------
dimOS (unmodified, called directly):
  * `WavefrontFrontierExplorer.get_exploration_goal(robot_pose, costmap)`: wavefront BFS
    frontier detection, centroiding, and the five-term score (information gain, distance
    from explored goals, lookahead distance, obstacle clearance, direction momentum) in
    `dimos/navigation/frontier_exploration/wavefront_frontier_goal_selector.py`.
  * `dimos.msgs.nav_msgs.OccupancyGrid` (ROS-convention costmap: -1 unknown, 0 free,
    100 occupied; `grid[y, x]`; `origin` is the world position of cell (0, 0)).
Ours (this file):
  * `to_dimos_costmap`: converts `robot/go2_smart_patrol.py::OccupancyGrid` (float obstacle
    evidence + visited footprint, indexed `[x, y]`, origin at the window centre) into that
    costmap, optionally ray-clearing free space around the current pose. Ray clearing is
    OUR approximation (see `_ray_clear`): our grid records where the dog walked, not what
    the LiDAR saw as empty, and dimOS frontiers need observed-free space.
  * `_OfflineRPC`: a no-op RPC transport. A dimOS `Module` otherwise opens a Zenoh (or LCM)
    session in its constructor (`dimos/core/module.py:157-164`); with this the explorer is
    a plain in-process object: no sockets, no threads of its own, nothing published.
  * yaw: dimOS never takes a heading. We seed its `exploration_direction` (the momentum
    term, 5 % of the score) from yaw when it has no direction yet.

The goal is a position to drive toward, NOT a path and NOT a velocity: dimOS hands goals to
its own planner (`replanning_a_star`) over `goal_request`. Annie has no such planner, so the
caller must still turn the goal into (vx, wz) under the existing collision guardrails.
"""
from __future__ import annotations

import math

import numpy as np

from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid as DimosOccupancyGrid
from dimos.navigation.frontier_exploration.wavefront_frontier_goal_selector import WavefrontFrontierExplorer
from dimos.protocol.rpc.spec import RPCSpec

# dimOS defaults (safe_distance 3 m, lookahead 5 m) assume building-scale maps; Annie's grid is a
# 12 m window of a home, so the defaults here are scaled down. All are WavefrontConfig fields.
HOME_SCALE_CONFIG = {"safe_distance": 1.0, "lookahead_distance": 2.0, "max_explored_distance": 6.0,
                     "min_frontier_perimeter": 0.5}


class _OfflineRPC(RPCSpec):
    """No-op RPC transport so a dimOS Module can be used as a plain object."""

    def __init__(self, **_kwargs):
        self.rpc_timeouts, self.default_rpc_timeout = {}, 1.0

    def serve_rpc(self, f, name):
        return lambda: None

    def call(self, name, arguments, cb=None):
        raise RuntimeError("offline dimOS module: no RPC transport")

    def call_nowait(self, name, arguments):
        raise RuntimeError("offline dimOS module: no RPC transport")


def _ray_clear(occupied_xy, pose_xy, origin_xy, cell_m, range_m, n_rays):
    """Cells (mask indexed [x, y]) on rays from the pose up to the first occupied cell or `range_m`.

    OURS, and an approximation: it treats "no obstacle evidence within range" as observed-free,
    which is what a LiDAR ray that returned nothing means only if the ray was actually cast.
    """
    n = occupied_xy.shape[0]
    steps = np.arange(cell_m * 0.5, range_m, cell_m * 0.5, dtype=np.float32)
    angles = np.linspace(-math.pi, math.pi, n_rays, endpoint=False, dtype=np.float32)
    xs = pose_xy[0] + np.outer(np.cos(angles), steps)
    ys = pose_xy[1] + np.outer(np.sin(angles), steps)
    ii = np.floor((xs - origin_xy[0]) / cell_m + n / 2).astype(int)
    jj = np.floor((ys - origin_xy[1]) / cell_m + n / 2).astype(int)
    inside = (ii >= 0) & (ii < n) & (jj >= 0) & (jj < n)
    hit = np.zeros_like(inside)
    hit[inside] = occupied_xy[ii[inside], jj[inside]]
    blocked = np.maximum.accumulate(hit | ~inside, axis=1)  # everything at and beyond the first hit / edge
    keep = ~blocked
    free = np.zeros_like(occupied_xy, dtype=bool)
    free[ii[keep], jj[keep]] = True
    return free


def to_dimos_costmap(grid, *, pose_xy=None, clear_range_m=0.0, n_rays=180) -> DimosOccupancyGrid:
    """Annie's patrol grid -> a dimOS `OccupancyGrid` costmap.

    `grid` is duck-typed: `obstacle` (float evidence, [x, y]), `visited` (0/1, [x, y]),
    `origin` (world xy of the window CENTRE), `cell_m`, `n`, `threshold`.
    occupied = evidence >= threshold; free = visited (or ray-cleared) and not occupied;
    everything else unknown. dimOS indexes `[y, x]`, hence the transpose; its origin is the
    world position of cell (0, 0), hence the half-window shift.
    """
    occupied = np.asarray(grid.obstacle) >= float(grid.threshold)
    free = np.asarray(grid.visited).astype(bool)
    if clear_range_m > 0.0 and pose_xy is not None:
        free = free | _ray_clear(occupied, pose_xy, grid.origin, grid.cell_m, clear_range_m, n_rays)
    cells = np.full(occupied.shape, int(CostValues.UNKNOWN), dtype=np.int8)
    cells[free] = int(CostValues.FREE)
    cells[occupied] = int(CostValues.OCCUPIED)
    half = grid.n * grid.cell_m / 2.0
    origin = Pose(grid.origin[0] - half, grid.origin[1] - half, 0.0)
    return DimosOccupancyGrid(grid=np.ascontiguousarray(cells.T), resolution=float(grid.cell_m),
                              origin=origin, frame_id="odom")


class FrontierPlanner:
    """Holds one dimOS explorer so its memory (explored goals, momentum, no-gain counter) persists."""

    def __init__(self, **wavefront_config):
        self.explorer = WavefrontFrontierExplorer(rpc_transport=_OfflineRPC, **{**HOME_SCALE_CONFIG, **wavefront_config})

    def next_goal(self, grid, pose_xy, yaw, *, clear_range_m=0.0, n_rays=180):
        costmap = to_dimos_costmap(grid, pose_xy=pose_xy, clear_range_m=clear_range_m, n_rays=n_rays)
        direction = self.explorer.exploration_direction
        if direction.x == 0 and direction.y == 0 and yaw is not None:
            self.explorer.exploration_direction = Vector3(math.cos(yaw), math.sin(yaw), 0.0)
        goal = self.explorer.get_exploration_goal(Vector3(float(pose_xy[0]), float(pose_xy[1]), 0.0), costmap)
        if goal is None:
            return None
        # dimOS returns a cell's lower corner (grid_to_world has no half-cell offset); centre it.
        return (float(goal.x) + grid.cell_m / 2.0, float(goal.y) + grid.cell_m / 2.0)

    def reset(self):
        self.explorer.reset_exploration_session()


def next_frontier_goal(occupancy_grid, pose_xy, yaw, *, planner: FrontierPlanner | None = None,
                       clear_range_m=0.0, n_rays=180):
    """World (x, y) of the best frontier to explore next, or None when dimOS finds none.

    Stateless unless a `FrontierPlanner` is passed: a fresh explorer has no explored-goal
    memory, so repeated calls on an unchanged grid return the same goal. Pass one planner
    for a whole patrol. Measured cost is reported in docs/DIMOS_INTEGRATION.md; it is a
    pure-Python BFS over the whole grid, so call it at goal rate (<= 1 Hz), not per control tick.
    """
    return (planner or FrontierPlanner()).next_goal(occupancy_grid, pose_xy, yaw,
                                                    clear_range_m=clear_range_m, n_rays=n_rays)
