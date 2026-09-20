"""Closed-loop waypoint missions over authored simulator collision geometry.

This is a small A* scene planner, not SLAM. It commands a trained locomotion
policy and measures actual MuJoCo pose; it never writes the robot pose.
"""

from __future__ import annotations

import heapq
import math
import time
from uuid import uuid4


def yaw_of(q):
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def angle(value):
    return (value + math.pi) % (2 * math.pi) - math.pi


class Navigator:
    def __init__(self, model, data, scene=None):
        import numpy as np

        self.model, self.data = model, data
        self.scene = scene or {}
        self.home = tuple(float(v) for v in data.qpos[:2])
        size = self.scene.get("ground_truth", {}).get("room_size_m", [6, 5])
        self.bounds = (
            -size[0] / 2 + 0.48,
            size[0] / 2 - 0.48,
            -size[1] / 2 + 0.48,
            size[1] / 2 - 0.48,
        )
        self.obstacles = []  # Inflated by the robot radius; used for planning.
        self.colliders = []  # Raw geom bounds; entering one is a hard fault.
        for i in range(model.ngeom):
            name = model.geom(i).name or ""
            if not name.startswith("env_"):
                continue
            # Residents block the footprint even though the person scan mesh is
            # visual-only (contype 0): the body mission must never overlap them.
            resident = name.startswith(("env_person_", "env_resident"))
            if resident and model.body_mocapid[model.geom_bodyid[i]] >= 0:
                # Animated actor protection is evaluated each physics tick by
                # the viewer; freezing its initial bounds makes a false map.
                continue
            if not resident and not (model.geom_contype[i] or model.geom_conaffinity[i]):
                continue
            # Use conservative transformed bounds for static obstacle shapes.
            # Planes and rugs do not block the robot's footprint.
            if int(model.geom_type[i]) == 0:
                continue
            rotation = data.geom_xmat[i].reshape(3, 3)
            if resident and int(model.geom_type[i]) == 7:
                # geom_size does not describe a mesh; use its local AABB.
                local_center = model.geom_aabb[i][:3]
                local_half = model.geom_aabb[i][3:]
                center = data.geom_xpos[i] + rotation @ local_center
                half = np.abs(rotation) @ local_half
            else:
                half = np.abs(rotation) @ model.geom_size[i]
                center = data.geom_xpos[i]
            if center[2] + half[2] < 0.12 or center[2] - half[2] > 0.7:
                continue
            box = (
                center[0] - half[0],
                center[0] + half[0],
                center[1] - half[1],
                center[1] + half[1],
            )
            self.colliders.append(box)
            self.obstacles.append(
                (box[0] - 0.38, box[1] + 0.38, box[2] - 0.38, box[3] + 0.38)
            )
        self.resolution = 0.12
        self.commands = []
        self.active = None
        self.path = []
        self.goals = []
        self.state = "idle"
        self.waypoint = None
        self.turn_yaw = None
        self.started = 0
        self.scan_yaw = None
        self.last_progress = (0.0, self.home)
        self.waypoints = []
        candidates = [
            ("home", self.home),
            ("living-room", (0.0, -0.2)),
            ("bedroom", (-0.55, 0.6)),
            ("hallway", (1.0, -1.25)),
        ]
        if self.scene.get('waypoints'):
            candidates = [('home', self.home)] + [(p['id'], (p['x'], p['y']))
                for p in self.scene['waypoints'] if p.get('floor', 0) == 0 and p['id'] != 'home']
        for name, target in candidates:
            point = self.nearest_free(target)
            if point is not None:
                self.waypoints.append({"id": name, "x": point[0], "y": point[1]})

    def free(self, point):
        x, y = point
        lo_x, hi_x, lo_y, hi_y = self.bounds
        return (
            lo_x <= x <= hi_x
            and lo_y <= y <= hi_y
            and not any(a <= x <= b and c <= y <= d for a, b, c, d in self.obstacles)
        )

    def blocked(self, point):
        """True when the measured base center leaves the room or enters a raw
        obstacle box. Planning uses the inflated map, so brushing the safety
        margin near a corner is not a fault; actual overlap is."""
        x, y = point
        lo_x, hi_x, lo_y, hi_y = self.bounds
        return (
            not (lo_x <= x <= hi_x and lo_y <= y <= hi_y)
            or any(a <= x <= b and c <= y <= d for a, b, c, d in self.colliders)
        )

    def nearest_free(self, point):
        options = []
        lo_x, hi_x, lo_y, hi_y = self.bounds
        for i in range(math.ceil((hi_x - lo_x) / self.resolution) + 1):
            for j in range(math.ceil((hi_y - lo_y) / self.resolution) + 1):
                p = (lo_x + i * self.resolution, lo_y + j * self.resolution)
                if self.free(p):
                    options.append((math.dist(p, point), p))
        return min(options)[1] if options else None

    def clear_line(self, a, b):
        n = max(1, math.ceil(math.dist(a, b) / 0.05))
        return all(
            self.free((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n))
            for i in range(n + 1)
        )

    def plan(self, start, end):
        origin = self.bounds[0], self.bounds[2]

        def cell(p):
            return tuple(round((p[i] - origin[i]) / self.resolution) for i in range(2))

        def point(c):
            return tuple(origin[i] + c[i] * self.resolution for i in range(2))

        start_cell, goal_cell = cell(start), cell(end)
        frontier = [(0.0, start_cell)]
        costs, parent = {start_cell: 0.0}, {}
        while frontier and len(costs) < 10000:
            _, current = heapq.heappop(frontier)
            if current == goal_cell:
                cells = [current]
                while cells[-1] != start_cell:
                    cells.append(parent[cells[-1]])
                route = [start] + [point(c) for c in reversed(cells)] + [end]
                smoothed = [route[0]]
                i = 0
                while i < len(route) - 1:
                    j = len(route) - 1
                    while j > i + 1 and not self.clear_line(route[i], route[j]):
                        j -= 1
                    smoothed.append(route[j])
                    i = j
                return smoothed[1:]
            for dx, dy in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
                (1, 1),
                (1, -1),
                (-1, 1),
                (-1, -1),
            ):
                nxt = current[0] + dx, current[1] + dy
                if not self.free(point(nxt)) or not self.clear_line(
                    point(current), point(nxt)
                ):
                    continue
                cost = costs[current] + math.hypot(dx, dy)
                if cost < costs.get(nxt, math.inf):
                    costs[nxt], parent[nxt] = cost, current
                    heapq.heappush(frontier, (cost + math.dist(nxt, goal_cell), nxt))
        raise ValueError("No collision-free path to waypoint")

    def update(self, status, detail):
        if self.active:
            self.active.update(status=status, detail=detail)
            if status == 'completed':
                self.active['completed_at'] = int(time.time() * 1000)

    def fail(self, detail):
        self.update("failed", detail)
        self.state = "failed"
        self.path = []

    def command(self, cmd, waypoint=None, command_id=None, heading=None):
        command_id = command_id or str(uuid4())
        # Duplicate suppression covers only the retained 100-command window;
        # upstream callers must dedupe retried IDs older than that.
        if any(c["command_id"] == command_id for c in self.commands):
            return
        if self.active and self.active["status"] in ("accepted", "executing"):
            self.update("failed", "Superseded by a new command")
        item = {
            "command_id": command_id,
            "cmd": cmd,
            "status": "accepted",
            "detail": "Simulator accepted command",
        }
        if cmd == 'goto':
            item['waypoint'] = waypoint
        self.commands = (self.commands + [item])[-100:]
        self.active = item
        self.started = float(self.data.time)
        self.last_progress = (self.started, tuple(self.data.qpos[:2]))
        try:
            if cmd == "stop":
                self.state = "stopped"
                self.update(
                    "completed",
                    "Zero-velocity request applied; active balance remains enabled",
                )
            elif cmd == "resume":
                if not self.path:
                    raise ValueError("No paused route to resume")
                self.state = "moving"
                self.update("executing", "Resuming stored route")
            elif cmd == "look":
                self.state = "scanning"
                self.scan_yaw = yaw_of(self.data.qpos[3:7])
                self.update("executing", "Turning robot to inspect the scene")
            elif cmd == "turn":
                # heading is an ABSOLUTE world yaw target in radians [-pi, pi],
                # not a relative offset; callers pre-add the desired offset.
                if (
                    isinstance(heading, bool)
                    or not isinstance(heading, (int, float))
                    or not math.isfinite(heading)
                ):
                    raise ValueError(
                        "Turn heading must be a finite absolute world yaw in radians"
                    )
                if not -math.pi <= heading <= math.pi:
                    raise ValueError("Turn heading must be within [-pi, pi] radians")
                self.state = "turning"
                self.turn_yaw = float(heading)
                self.update(
                    "executing", f"Turning to absolute yaw {heading:.2f} rad"
                )
            else:
                self.goals = (
                    ["living-room", "bedroom", "hallway", "home"]
                    if cmd == "patrol"
                    else [waypoint]
                )
                self.next_goal()
        except ValueError as exc:
            self.fail(str(exc))

    def next_goal(self):
        if not self.goals:
            self.state = "idle"
            self.update(
                "completed", "Reached all requested waypoints using measured robot pose"
            )
            return
        # Peek, validate and plan first so a failure leaves the goal queued.
        target = next((w for w in self.waypoints if w["id"] == self.goals[0]), None)
        if target is None:
            raise ValueError("Unknown waypoint")
        path = self.plan(tuple(self.data.qpos[:2]), (target["x"], target["y"]))
        self.waypoint = self.goals.pop(0)
        self.path = path
        self.state = "moving"
        self.update("executing", f"Walking to {self.waypoint}")

    def velocity(self):
        if self.state not in ("moving", "scanning", "turning"):
            return 0.0, 0.0, 0.0
        now = float(self.data.time)
        if now - self.started > 150:
            self.fail("Mission exceeded 150 simulation seconds")
            return 0.0, 0.0, 0.0
        yaw = yaw_of(self.data.qpos[3:7])
        # The person/furniture stop guard applies to rotation missions too;
        # a base that leaves the room or overlaps a resident stops every state.
        p = tuple(self.data.qpos[:2])
        if self.data.qpos[2] < 0.17 or self.blocked(p):
            self.fail("Robot left the room, entered an obstacle, or lost standing height")
            return 0.0, 0.0, 0.0
        if self.state == "scanning":
            elapsed = now - self.started
            if elapsed >= 12:
                self.state = "idle"
                self.update(
                    "completed",
                    "Robot scan finished; camera frames are available for inference",
                )
                return 0.0, 0.0, 0.0
            target = self.scan_yaw + (
                0.65 if elapsed < 4 else -0.65 if elapsed < 9 else 0
            )
            return 0.0, 0.0, max(-1.0, min(1.0, 3 * angle(target - yaw)))
        if self.state == "turning":
            error = angle(self.turn_yaw - yaw)
            if abs(error) < 0.12:
                self.state = "idle"
                self.update(
                    "completed",
                    f"Turn finished with {error:.2f} rad measured yaw error",
                )
                return 0.0, 0.0, 0.0
            return 0.0, 0.0, max(-1.0, min(1.0, 3 * error))
        if now - self.last_progress[0] > 25:
            if math.dist(p, self.last_progress[1]) < 0.08:
                self.fail("No measured progress toward waypoint")
                return 0.0, 0.0, 0.0
            self.last_progress = now, p
        while self.path and math.dist(p, self.path[0]) < 0.20:
            self.path.pop(0)
        if not self.path:
            try:
                self.next_goal()
            except ValueError as exc:
                self.fail(str(exc))
            return 0.0, 0.0, 0.0
        target = self.path[0]
        direction = math.atan2(target[1] - p[1], target[0] - p[0])
        error = angle(direction - yaw)
        forward = max(0.28, min(0.35, 0.65 * math.dist(p, target))) * max(0.0, math.cos(error))
        if abs(error) > 0.8:
            forward = 0.0
        return forward, 0.0, max(-1.0, min(1.0, 3 * error))

    def snapshot(self):
        return {
            "state": self.state,
            "waypoint": self.waypoint,
            "waypoints": self.waypoints,
            "commands": self.commands,
            "path": self.path,
            "source": "authored_collision_map_astar",
            "active_command": self.active,
        }
