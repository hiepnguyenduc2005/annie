"""SimDog: a kinematic Go2 in the furnished MuJoCo apartment, behind the WebRTC driver's interface.

The patrol process (`robot/dog/runtime/patrol.py`) talks to a connection object: `connect()`,
`datachannel.pub_sub.subscribe/publish_request_new/publish_without_callback`, `video.add_track_callback`.
SimDog implements that surface so the whole stack (command center, errand, family backend, phone app)
keeps running with the physical dog switched off:

    PY=.cache/dimos/.venv/bin/python
    $PY robot/go2_patrol_greet.py --sim seated --duration 600 --view-port 8011

What is simulated, and how (be precise when describing results):
  * motion      kinematic: the commanded Move (vx, vy, wz) is low-pass filtered and integrated into a
                planar pose. No legs, no dynamics, no gait policy, no slipping. A 0.70 x 0.31 m body
                rectangle is tested against a 2 cm raster of the scene between 0.10 and 0.80 m; a move
                that would overlap it is refused, so odometry stops advancing (the stall detector's job).
  * odometry    `rt/utlidar/robot_pose` in the apartment's world frame (odom == MuJoCo world), with the
                yaw quaternion. No drift.
  * LiDAR       `rt/utlidar/voxel_map_compressed` already decoded, in the driver's layout: uint8 cell
                positions relative to `origin`, 0.05 m cells, four vertices per face. Occupied cells are
                surface samples of the scene geoms (walls, furniture, people) cropped to 4 m around the dog.
                It is a geometric crop, not a ray cast: the map also holds what is behind a wall.
  * camera      MuJoCo offscreen render of `robot_front` (640x480, the real lens is approximated by
                fovy 85 deg) delivered as frames with `.to_ndarray(format="bgr24")`.
  * sport API   acks with the firmware's codes; tricks hold the body for a few seconds and ignore Move.
  * battery     `rt/lf/lowstate` soc drains slowly from 88 %.

People are the capsule mannequins of `robot/simulation/apartment.py`; `set_person_pose` re-poses them
(standing / sitting / lying / absent) and the camera, the voxel map and the collision raster follow.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot.simulation import apartment  # noqa: E402

TOPIC_SPORT = "rt/api/sport/request"
TOPIC_LOWSTATE = "rt/lf/lowstate"
TOPIC_POSE = "rt/utlidar/robot_pose"
TOPIC_VOXELS = "rt/utlidar/voxel_map_compressed"
TOPIC_SPORT_STATE = "rt/lf/sportmodestate"
TOPIC_MOTION_SWITCHER = "rt/api/motion_switcher/request"

BODY_LENGTH_M, BODY_WIDTH_M = 0.70, 0.31
COLLISION_BAND_M = (0.10, 0.80)   # geometry in this height band blocks the body
RASTER_RES_M = 0.02
VOXEL_RES_M = 0.05
VOXEL_RANGE_M = 4.0               # half-width of the published map; 160 cells, inside uint8
VOXEL_TOP_M = 1.0                 # the patrol's obstacle band ends at 0.75 m; nothing above 1 m is published
VOXEL_FLOOR_CELL = -1             # floor voxels sit just below z = 0, so their centres are at -0.025 m
RENDER_W, RENDER_H = 640, 480
RENDER_BASE_Z = 0.27              # menagerie Go2 standing height used by the renders
POSE_Z = {"stand": 0.32, "sit": 0.22, "down": 0.10}  # reported odometry height (patrol.BASE_HEIGHT_M = 0.32)
MOVE_TIMEOUT_S = 1.0              # a Move that is not refreshed stops, as on the robot
MOVE_TAU_S = 0.20                 # first-order response of the body to a velocity command
MOVE_LIMITS = (1.0, 0.6, 2.0)     # |vx| m/s, |vy| m/s, |wz| rad/s

# Sport api id -> (name, seconds the body is busy and ignores Move, posture afterwards or None).
SPORT_ACTIONS = {
    1001: ("Damp", 0.5, "down"), 1002: ("BalanceStand", 0.3, "stand"), 1004: ("StandUp", 1.5, "stand"),
    1005: ("StandDown", 2.0, "down"), 1006: ("RecoveryStand", 2.0, "stand"), 1009: ("Sit", 2.0, "sit"),
    1010: ("RiseSit", 2.0, "stand"), 1016: ("Hello", 3.0, None), 1017: ("Stretch", 4.0, None),
    1020: ("Content", 3.0, None), 1022: ("Dance1", 8.0, None), 1023: ("Dance2", 8.0, None),
    1029: ("Scrape", 4.0, None), 1031: ("FrontJump", 2.0, None), 1032: ("FrontPounce", 3.0, None),
    1033: ("WiggleHips", 4.0, None), 1036: ("Heart", 4.0, None),
}
STOP_MOVE, EULER, MOVE = 1003, 1007, 1008
ALWAYS_ACKED = {1001, 1002, 1003}                    # safe commands are accepted even mid-trick
REFUSED_APIS = {1030, 1042, 1043, 1044, 1301}        # flips / handstand: this firmware answers 3203
CODE_OK, CODE_BUSY, CODE_NOT_IMPLEMENTED = 0, -1, 3203
TRICK_PITCH = {"Hello": -0.30, "Heart": -0.30, "Stretch": 0.18, "Sit": -0.35}  # cosmetic camera attitude (rad, nose up < 0)

# Mirrors the residents placed by apartment.build(): name -> (posture, x, y, yaw_deg) per variant.
PEOPLE_BUILD = {"jeanine": {"height": 1.60}, "visitor": {"height": 1.78, "mug": True}}
PEOPLE_START = {
    "seated": {"jeanine": ("sitting", -1.45, 1.78, -90.0), "visitor": ("standing", 0.55, -0.45, 196.0)},
    "floor": {"jeanine": ("lying", -1.3, 0.1, 8.0)},
    "empty": {},
}
POSTURES = ("standing", "sitting", "lying", "absent")
SEAT_HEIGHT_M = 0.40
# Inside the living room (the scene's keyframe starts in the hallway, behind a 0.9 m doorway): x, y, yaw_deg.
DEFAULT_START = (-2.6, -1.25, 35.0)
DEFAULT_CONTROL_FILE = ".data/sim/control.jsonl"


def _response(code, data=None):
    body = {"header": {"status": {"code": int(code)}}}
    if data is not None:
        body["data"] = json.dumps(data)
    return {"type": "res", "data": body}


def yaw_quaternion(yaw, pitch=0.0, roll=0.0):
    """(x, y, z, w) for intrinsic yaw-pitch-roll; with pitch = roll = 0 it is the planar heading."""
    cy, sy, cp, sp, cr, sr = (math.cos(yaw / 2), math.sin(yaw / 2), math.cos(pitch / 2), math.sin(pitch / 2),
                              math.cos(roll / 2), math.sin(roll / 2))
    return (sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy)


class SimFrame:
    """The slice of `av.VideoFrame` the dog stack uses."""

    def __init__(self, bgr, seq, t_s):
        self._bgr, self.seq, self.time = bgr, seq, t_s
        self.pts = int(t_s * 90000)
        self.height, self.width = bgr.shape[:2]

    def to_ndarray(self, format="bgr24"):  # noqa: A002  (av's keyword)
        if format == "bgr24":
            return self._bgr
        if format == "rgb24":
            return np.ascontiguousarray(self._bgr[:, :, ::-1])
        raise ValueError(f"unsupported frame format {format!r}")


# --------------------------------------------------------------------------- scene geometry

class _PoseRecorder:
    """Stands in for apartment.Scene so apartment.person() yields geom shapes instead of MJCF."""

    def __init__(self):
        self.shapes = {}

    def capsule(self, name, a, b, radius, material):
        self.shapes["env_" + name] = (np.asarray(a, float), np.asarray(b, float), float(radius))

    cylinder = capsule

    def sphere(self, name, pos, radius, material):
        self.shapes["env_" + name] = (np.asarray(pos, float), None, float(radius))

    def flat(self, name, *args, **kwargs):
        return name


def _unit_sphere(n):
    i = np.arange(n) + 0.5
    polar, azimuth = np.arccos(1 - 2 * i / n), np.pi * (1 + 5 ** 0.5) * i
    return np.stack([np.sin(polar) * np.cos(azimuth), np.sin(polar) * np.sin(azimuth), np.cos(polar)], axis=1)


def _grid(half, step):
    return np.linspace(-half, half, max(2, int(math.ceil(2 * half / step)) + 1))


def _primitive_points(mj, gtype, size, step):
    """Surface samples of a primitive geom in its own frame, roughly `step` apart."""
    g, gtype = {k: int(getattr(mj.mjtGeom, "mjGEOM_" + k)) for k in ("BOX", "SPHERE", "ELLIPSOID", "CAPSULE", "CYLINDER")}, int(gtype)
    if gtype == g["BOX"]:
        faces = []
        for axis in range(3):
            u, v = [a for a in range(3) if a != axis]
            uu, vv = np.meshgrid(_grid(size[u], step), _grid(size[v], step), indexing="ij")
            for sign in (-1.0, 1.0):
                pts = np.zeros((uu.size, 3))
                pts[:, u], pts[:, v], pts[:, axis] = uu.ravel(), vv.ravel(), sign * size[axis]
                faces.append(pts)
        return np.concatenate(faces)
    if gtype in (g["SPHERE"], g["ELLIPSOID"]):
        radii = np.array([size[0]] * 3 if gtype == g["SPHERE"] else size[:3])
        return _unit_sphere(max(24, int(4 * math.pi * float(radii.max()) ** 2 / step ** 2))) * radii
    if gtype in (g["CAPSULE"], g["CYLINDER"]):
        radius, half = float(size[0]), float(size[1])
        theta = np.linspace(0, 2 * math.pi, max(8, int(math.ceil(2 * math.pi * radius / step))), endpoint=False)
        tt, zz = np.meshgrid(theta, _grid(half, step) if half > 0 else np.zeros(1), indexing="ij")
        parts = [np.stack([radius * np.cos(tt).ravel(), radius * np.sin(tt).ravel(), zz.ravel()], axis=1)]
        if gtype == g["CAPSULE"]:
            cap = _unit_sphere(max(24, int(4 * math.pi * radius ** 2 / step ** 2))) * radius
            parts += [cap[cap[:, 2] >= 0] + (0, 0, half), cap[cap[:, 2] <= 0] - (0, 0, half)]
        else:
            uu, vv = np.meshgrid(_grid(radius, step), _grid(radius, step), indexing="ij")
            disc = np.stack([uu.ravel(), vv.ravel()], axis=1)
            disc = disc[np.hypot(disc[:, 0], disc[:, 1]) <= radius]
            parts += [np.column_stack([disc, np.full(len(disc), s * half)]) for s in (-1.0, 1.0)]
        return np.concatenate(parts)
    return np.zeros((0, 3))


class ApartmentGeometry:
    """Voxel cells and a collision raster derived from the compiled apartment's `env_*` geoms."""

    def __init__(self, model, rng, *, step=0.025):
        import mujoco
        self.mj, self.model, self.rng, self.step = mujoco, model, rng, step
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        self.people_geoms: dict[str, list[int]] = {}
        static, planes = [], []
        for gid in range(model.ngeom):
            name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, gid) or ""
            if not name.startswith("env_"):
                continue  # the Go2's own geoms
            if name.startswith("env_person_"):
                self.people_geoms.setdefault(name.split("_")[2], []).append(gid)
            elif int(model.geom_type[gid]) == int(mujoco.mjtGeom.mjGEOM_PLANE):
                planes.append(gid)
            else:
                static.append(gid)
        cells, hulls = [self._floor_cells(planes)], []
        for gid in static:
            pts = self._world_points(gid, data.geom_xpos[gid], data.geom_xmat[gid].reshape(3, 3))
            cells.append(self._cells(pts))
            if int(model.geom_type[gid]) != int(mujoco.mjtGeom.mjGEOM_MESH):  # furniture collides through its box proxy
                hulls.append(self._band(pts))
        self.static_cells = self._unique(np.concatenate(cells))
        hull_pts = np.concatenate([h for h in hulls if len(h)])
        self.x0, self.y0 = hull_pts[:, :2].min(axis=0) - 0.5
        span = hull_pts[:, :2].max(axis=0) + 0.5 - (self.x0, self.y0)
        self.shape = (int(math.ceil(span[1] / RASTER_RES_M)), int(math.ceil(span[0] / RASTER_RES_M)))  # rows = y, cols = x
        self.static_raster = np.zeros(self.shape, dtype=np.uint8)
        for hull in hulls:
            self._fill(self.static_raster, hull)
        self.refresh_people()

    # -- sampling
    def _world_points(self, gid, pos, mat):
        m, mj = self.model, self.mj
        if int(m.geom_type[gid]) == int(mj.mjtGeom.mjGEOM_MESH):
            mesh = m.geom_dataid[gid]
            verts = m.mesh_vert[m.mesh_vertadr[mesh]: m.mesh_vertadr[mesh] + m.mesh_vertnum[mesh]] @ mat.T + pos
            faces = m.mesh_face[m.mesh_faceadr[mesh]: m.mesh_faceadr[mesh] + m.mesh_facenum[mesh]]
            tri = verts[faces]
            tri = tri[tri[:, :, 2].min(axis=1) <= VOXEL_TOP_M]
            if not len(tri):
                return np.zeros((0, 3))
            area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
            count = np.clip(np.ceil(area / (self.step * 1.2) ** 2), 1, 4000).astype(int)
            idx = np.repeat(np.arange(len(tri)), count)
            r1, r2 = np.sqrt(self.rng.random(len(idx))), self.rng.random(len(idx))
            a, b, c = tri[idx, 0], tri[idx, 1], tri[idx, 2]
            return (1 - r1)[:, None] * a + (r1 * (1 - r2))[:, None] * b + (r1 * r2)[:, None] * c
        return _primitive_points(mj, m.geom_type[gid], m.geom_size[gid], self.step) @ mat.T + pos

    def _floor_cells(self, planes):
        out = []
        for gid in planes:
            (cx, cy, _), (hx, hy, _) = self.model.geom_pos[gid], self.model.geom_size[gid]
            xs = np.arange(math.floor((cx - hx) / VOXEL_RES_M), math.ceil((cx + hx) / VOXEL_RES_M))
            ys = np.arange(math.floor((cy - hy) / VOXEL_RES_M), math.ceil((cy + hy) / VOXEL_RES_M))
            xx, yy = np.meshgrid(xs, ys, indexing="ij")
            out.append(np.stack([xx.ravel(), yy.ravel(), np.full(xx.size, VOXEL_FLOOR_CELL)], axis=1))
        return np.concatenate(out).astype(np.int32) if out else np.zeros((0, 3), np.int32)

    @staticmethod
    def _cells(pts):
        pts = pts[(pts[:, 2] >= 0.0) & (pts[:, 2] < VOXEL_TOP_M)]
        return np.floor(pts / VOXEL_RES_M).astype(np.int32)

    @staticmethod
    def _unique(cells):
        if not len(cells):
            return cells.reshape(0, 3).astype(np.int32)
        shifted = cells.astype(np.int64) + 100_000
        keys = np.unique((shifted[:, 0] << 40) | (shifted[:, 1] << 20) | shifted[:, 2])
        return (np.stack([keys >> 40, (keys >> 20) & 0xFFFFF, keys & 0xFFFFF], axis=1) - 100_000).astype(np.int32)

    @staticmethod
    def _band(pts):
        return pts[(pts[:, 2] > COLLISION_BAND_M[0] + 1e-6) & (pts[:, 2] < COLLISION_BAND_M[1])]

    def _fill(self, raster, pts):
        """A primitive's slice of the height band is convex: fill the hull of its projected samples."""
        import cv2
        if not len(pts):
            return
        px = np.round((pts[:, :2] - (self.x0, self.y0)) / RASTER_RES_M).astype(np.int32)
        hull = cv2.convexHull(px.reshape(-1, 1, 2))
        if len(hull) >= 3:
            cv2.fillConvexPoly(raster, hull, 1)
        for x, y in hull.reshape(-1, 2):
            cv2.circle(raster, (int(x), int(y)), 1, 1, -1)

    # -- people
    def pose_person(self, name, posture, x, y, yaw_deg):
        """Rewrite one mannequin's geoms in the model for a posture at a floor point."""
        m, mj = self.model, self.mj
        gids = self.people_geoms[name]
        if posture == "absent":
            for gid in gids:
                m.geom_pos[gid] = (x, y, -50.0)
            return
        build = PEOPLE_BUILD.get(name, {})
        rec = _PoseRecorder()
        apartment.person(rec, name, posture, (x, y, 0.0), yaw_deg, height=build.get("height", 1.72), shirt=None,
                         trousers=None, hair=None, skin=None, shoes=None, mug=build.get("mug", False),
                         seat_height=SEAT_HEIGHT_M if posture == "sitting" else 0.0)
        for gid in gids:
            a, b, radius = rec.shapes[mj.mj_id2name(m, mj.mjtObj.mjOBJ_GEOM, gid)]
            if b is None:
                m.geom_pos[gid], m.geom_size[gid], half = a, (radius, 0, 0), 0.0
            else:
                axis, quat = b - a, np.zeros(4)
                half = float(np.linalg.norm(axis)) / 2
                mj.mju_quatZ2Vec(quat, axis / max(2 * half, 1e-9))
                m.geom_pos[gid], m.geom_quat[gid], m.geom_size[gid] = (a + b) / 2, quat, (radius, half, 0)
            m.geom_rbound[gid] = radius + half
            m.geom_aabb[gid] = (0, 0, 0, radius, radius, radius + half)

    def refresh_people(self):
        """Recompute the voxel cells and the raster after people moved. World geoms: pose == geom_pos/quat."""
        cells, raster = [self.static_cells], self.static_raster.copy()
        for gids in self.people_geoms.values():
            for gid in gids:
                if self.model.geom_pos[gid][2] < -1.0:
                    continue  # absent
                mat = np.zeros(9)
                self.mj.mju_quat2Mat(mat, self.model.geom_quat[gid])
                pts = self._world_points(gid, self.model.geom_pos[gid], mat.reshape(3, 3))
                cells.append(self._cells(pts))
                self._fill(raster, self._band(pts))
        self.cells, self.raster = self._unique(np.concatenate(cells)), raster

    # -- queries
    def blocked(self, pts_xy):
        """True when any of the world points lies on occupied raster cells (or outside the mapped flat)."""
        idx = np.floor((pts_xy - (self.x0, self.y0)) / RASTER_RES_M).astype(int)
        cols, rows = idx[:, 0], idx[:, 1]
        if (cols < 0).any() or (rows < 0).any() or (cols >= self.shape[1]).any() or (rows >= self.shape[0]).any():
            return True
        return bool(self.raster[rows, cols].any())

    def voxel_data(self, x, y, stamp):
        """The driver's decoded voxel message body for a dog at (x, y): a 2*RANGE square, circular crop."""
        n = int(round(2 * VOXEL_RANGE_M / VOXEL_RES_M))
        origin_cell = np.array([math.floor((x - VOXEL_RANGE_M) / VOXEL_RES_M), math.floor((y - VOXEL_RANGE_M) / VOXEL_RES_M),
                                VOXEL_FLOOR_CELL])
        rel = self.cells - origin_cell
        centre = (rel[:, :2] + 0.5) * VOXEL_RES_M + origin_cell[:2] * VOXEL_RES_M - (x, y)
        keep = ((rel[:, 0] >= 0) & (rel[:, 0] < n) & (rel[:, 1] >= 0) & (rel[:, 1] < n)
                & (np.hypot(centre[:, 0], centre[:, 1]) <= VOXEL_RANGE_M))
        cells = rel[keep].astype(np.uint8)
        top = int(round(VOXEL_TOP_M / VOXEL_RES_M)) + 1
        return {"stamp": stamp, "frame_id": "odom", "resolution": VOXEL_RES_M,
                "origin": [float(v) * VOXEL_RES_M for v in origin_cell], "width": [n, n, top],
                # libvoxel hands back four uint8 vertices per face; the patrol keeps the first of each four
                "data": {"point_count": int(len(cells)), "face_count": int(len(cells)),
                         "positions": np.repeat(cells, 4, axis=0).reshape(-1)}}


# --------------------------------------------------------------------------- the connection surface

class _Track:
    def __init__(self, dog):
        self.dog, self.seen, self.kind = dog, 0, "video"

    async def recv(self):
        while True:
            frame = self.dog._frame
            if frame is not None and frame.seq != self.seen:
                self.seen = frame.seq
                return frame
            if self.dog.disconnected:
                raise ConnectionError("simulated dog disconnected")
            await asyncio.sleep(0.004)


class _Video:
    def __init__(self, dog):
        self.dog = dog

    def add_track_callback(self, callback):
        self.dog._track_tasks.append(asyncio.ensure_future(callback(_Track(self.dog))))


class _PubSub:
    def __init__(self, dog):
        self.dog = dog

    def subscribe(self, topic, callback=None):
        self.dog.subs[topic] = callback

    def unsubscribe(self, topic):
        self.dog.subs.pop(topic, None)

    async def publish_request_new(self, topic, options=None):
        options = options or {}
        self.dog.requests.append((topic, options))
        await asyncio.sleep(0.02 + 0.03 * self.dog._jitter.random())  # data-channel round trip
        return self.dog._handle_request(topic, options)

    def publish_without_callback(self, topic, data=None, msg_type=None):
        self.dog.sent.append((topic, data, msg_type))
        if topic == TOPIC_SPORT and isinstance(data, dict):
            api = ((data.get("header") or {}).get("identity") or {}).get("api_id", MOVE)
            if api == MOVE:
                self.dog._on_move(data.get("parameter"))


class _DataChannel:
    def __init__(self, dog):
        self.pub_sub = _PubSub(dog)

    async def disableTrafficSaving(self, switch):  # noqa: N802  (driver's name)
        return True

    def switchVideoChannel(self, switch):  # noqa: N802
        pass

    def switchAudioChannel(self, switch):  # noqa: N802
        pass


class SimDog:
    """A Go2 stand-in driven by the apartment scene. One instance is one connection (reusable after disconnect)."""

    def __init__(self, variant="seated", rate_hz=10, camera_hz=10, seed=0, *, start=None, render=True, scenario=None,
                 control_file=None, soc=88.0):
        if variant not in apartment.VARIANTS:
            raise ValueError(f"variant must be one of {apartment.VARIANTS}")
        self.variant, self.rate_hz, self.camera_hz, self.render_enabled = variant, float(rate_hz), float(camera_hz), render
        self.rng = np.random.default_rng(seed)
        self._jitter = random.Random(seed)  # ack latency, drawn on the event loop (the numpy generator belongs to the body)
        self.model = apartment.load_model(variant)  # MjModel with the robot_front camera on the Go2 base
        self._lock = threading.RLock()              # model geoms + derived geometry (render thread vs set_person_pose)
        self.geometry = ApartmentGeometry(self.model, self.rng)
        self.people = {name: {"posture": p, "x": x, "y": y, "yaw_deg": yaw} for name, (p, x, y, yaw) in PEOPLE_START[variant].items()}
        sx, sy, syaw = start if start is not None else DEFAULT_START
        self.x, self.y, self.yaw = float(sx), float(sy), math.radians(syaw)
        self.vel = np.zeros(3)   # filtered body-frame vx, vy and yaw rate
        self._cmd = (np.zeros(3), -1e9)  # (body-frame velocity command, sim time it arrived): one tuple, swapped atomically
        self.t = 0.0             # simulated seconds
        self.soc, self.posture, self.pitch = float(soc), "stand", 0.0
        self.busy_until, self.action = 0.0, None
        self.travelled, self.contacts, self.in_contact = 0.0, 0, False
        self.scenario = self._parse_scenario(scenario) if isinstance(scenario, str) else scenario
        self._events_done: set[int] = set()
        self.control_file, self._control_offset = (Path(control_file) if control_file else None), 0
        half_l, half_w = BODY_LENGTH_M / 2, BODY_WIDTH_M / 2
        bx, by = np.meshgrid(_grid(half_l, 0.025), _grid(half_w, 0.025), indexing="ij")
        self._footprint = np.stack([bx.ravel(), by.ravel()], axis=1)
        if self.geometry.blocked(self._footprint_at(self.x, self.y, self.yaw)):
            raise ValueError(f"start pose {(sx, sy, syaw)} overlaps the scene")
        self.subs, self.requests, self.sent = {}, collections.deque(maxlen=4000), collections.deque(maxlen=4000)
        self.datachannel, self.video = _DataChannel(self), _Video(self)
        self.stats = {"ticks": 0, "frames": 0, "render_ms": None, "voxel_ms": None, "voxel_cells": None}
        self._frame, self._frame_seq, self._closed, self.disconnected = None, 0, True, False
        self._clock_thread, self._render_thread, self._track_tasks, self._stop = None, None, [], threading.Event()

    # ------------------------------------------------------------------ factory / CLI
    def connect_factory(self):
        """`conn_factory(ip, key)` for run_patrol_greet: every (re)connect attempt gets this same dog."""
        return lambda ip=None, aes_key=None: self

    @classmethod
    def from_cli(cls, variant, **kwargs):
        """The `--sim` flag's dog. Optional environment: ANNIE_SIM_START="x,y,yaw_deg",
        ANNIE_SIM_SCENARIO="120:jeanine:lying:-1.3,0.1,8;..." and ANNIE_SIM_CONTROL (a jsonl file polled for live commands)."""
        env = os.environ
        if env.get("ANNIE_SIM_START"):
            kwargs.setdefault("start", tuple(float(v) for v in env["ANNIE_SIM_START"].split(",")))
        kwargs.setdefault("scenario", env.get("ANNIE_SIM_SCENARIO") or None)
        kwargs.setdefault("control_file", env.get("ANNIE_SIM_CONTROL", DEFAULT_CONTROL_FILE))
        return cls(variant, **kwargs)

    @staticmethod
    def _parse_scenario(text):
        """"T:NAME:POSTURE:x,y,yaw_deg;..." -> [(t_s, name, posture, x, y, yaw_deg)]."""
        events = []
        for item in filter(None, (s.strip() for s in text.split(";"))):
            t_s, name, posture, where = item.split(":")
            x, y, yaw = (float(v) for v in where.split(","))
            events.append((float(t_s), name, posture, x, y, yaw))
        return events

    # ------------------------------------------------------------------ connection
    async def connect(self):
        self._closed, self.disconnected = False, False
        self._stop.clear()
        if self.control_file is not None:  # only commands written after this start are obeyed
            with contextlib.suppress(OSError):
                self.control_file.parent.mkdir(parents=True, exist_ok=True)
                self.control_file.touch()
                self._control_offset = self.control_file.stat().st_size
        if self.render_enabled:
            self._render_thread = threading.Thread(target=self._render_loop, daemon=True, name="simdog-camera")
            self._render_thread.start()
        self._clock_thread = threading.Thread(target=self._clock_loop, args=(asyncio.get_running_loop(),), daemon=True,
                                              name="simdog-clock")
        self._clock_thread.start()

    async def disconnect(self):
        self._closed, self.disconnected = True, True
        self._stop.set()
        for task in self._track_tasks:
            task.cancel()
        self._track_tasks = []
        for thread in (self._clock_thread, self._render_thread):
            if thread is not None:
                await asyncio.get_running_loop().run_in_executor(None, thread.join, 3.0)
        self._clock_thread = self._render_thread = None

    def _clock_loop(self, loop):
        """Real-time clock on its own thread: step the body with the measured wall interval, build the telemetry
        here and queue its delivery on the event loop. Queued callbacks run before the loop's due timers, as the
        driver's socket reads do, so a stalled loop finds fresh telemetry before the patrol's next tick judges it stale."""
        period, last = 1.0 / self.rate_hz, time.monotonic()
        deadline, next_control = last + period, 0.0
        while not self._stop.wait(max(0.0, deadline - time.monotonic())):
            now = time.monotonic()
            deadline = max(deadline + period, now)  # no burst of catch-up ticks after a stall
            self.step(min(now - last, 0.5))
            last = now
            if not self.render_enabled and self.t * self.camera_hz >= self._frame_seq:
                self._publish_frame(np.zeros((RENDER_H, RENDER_W, 3), np.uint8))
            if self.control_file is not None and now >= next_control:
                next_control = now + 0.5
                self._poll_control()
            try:
                loop.call_soon_threadsafe(self._deliver, self.messages())
            except RuntimeError:  # the loop is gone
                return

    # ------------------------------------------------------------------ body
    def _footprint_at(self, x, y, yaw):
        c, s = math.cos(yaw), math.sin(yaw)
        return self._footprint @ np.array([[c, s], [-s, c]]) + (x, y)

    def _on_move(self, parameter):
        try:
            p = json.loads(parameter) if isinstance(parameter, str) else dict(parameter or {})
            cmd = np.array([float(p.get("x", 0.0)), float(p.get("y", 0.0)), float(p.get("z", 0.0))])
        except (TypeError, ValueError):
            return
        if np.all(np.isfinite(cmd)):
            self._cmd = (np.clip(cmd, [-v for v in MOVE_LIMITS], MOVE_LIMITS), self.t)

    def command(self, vx=0.0, vy=0.0, wz=0.0):
        """Direct Move for tests that step the dog without an event loop."""
        self._on_move({"x": vx, "y": vy, "z": wz})

    @property
    def busy(self):
        return self.t < self.busy_until

    def step(self, dt):
        """Advance the simulated body by `dt` seconds."""
        self.t += dt
        self._run_scenario()
        if self.action and not self.busy:
            self.action = None
        cmd, cmd_t = self._cmd
        walking = self.posture == "stand" and not self.busy and self.t - cmd_t <= MOVE_TIMEOUT_S
        target = cmd if walking else np.zeros(3)
        vel = self.vel + (target - self.vel) * (1.0 - math.exp(-dt / MOVE_TAU_S))  # a fresh array: handlers on the loop rebind self.vel
        if not walking and np.abs(vel).max() < 1e-3:
            vel = np.zeros(3)
        vx, vy, wz = vel * (1.0 + 0.02 * self.rng.standard_normal(3)) if vel.any() else vel
        mid = self.yaw + wz * dt / 2
        nx = self.x + (vx * math.cos(mid) - vy * math.sin(mid)) * dt
        ny = self.y + (vx * math.sin(mid) + vy * math.cos(mid)) * dt
        nyaw = self.yaw + wz * dt
        with self._lock:
            geometry = self.geometry
            if not geometry.blocked(self._footprint_at(nx, ny, nyaw)):
                moved = (nx, ny, nyaw)
            elif not geometry.blocked(self._footprint_at(self.x, self.y, nyaw)):
                moved = (self.x, self.y, nyaw)      # pinned against something: it can still turn on the spot
            elif not geometry.blocked(self._footprint_at(nx, ny, self.yaw)):
                moved = (nx, ny, self.yaw)
            else:
                moved = (self.x, self.y, self.yaw)
        touching = (moved[0], moved[1]) != (nx, ny) and math.hypot(nx - self.x, ny - self.y) > 1e-6
        if touching:
            vel[:2] = 0.0
            if not self.in_contact:
                self.contacts += 1
        self.in_contact, self.vel = touching, vel
        self.travelled += math.hypot(moved[0] - self.x, moved[1] - self.y)
        self.x, self.y = float(moved[0]), float(moved[1])
        self.yaw = float((moved[2] + math.pi) % (2 * math.pi) - math.pi)
        moving = abs(vx) + abs(vy) + abs(wz) > 0.02
        # 1 % per 5 min walking, per 15 min standing: visible over a demo, hours before the patrol's 40 % floor
        self.soc = max(0.0, self.soc - dt * (1.0 / 300.0 if moving else 1.0 / 900.0))
        self.stats["ticks"] += 1

    def teleport(self, x, y, yaw_deg):
        if self.geometry.blocked(self._footprint_at(x, y, math.radians(yaw_deg))):
            raise ValueError("teleport target overlaps the scene")
        self.x, self.y, self.yaw, self.vel = float(x), float(y), math.radians(yaw_deg), np.zeros(3)

    def set_battery(self, soc):
        self.soc = min(100.0, max(0.0, float(soc)))

    # ------------------------------------------------------------------ people / scenarios
    def set_person_pose(self, name, x, y, yaw, posture="standing"):
        """Re-pose a resident of this variant. `yaw` in degrees (the scene's convention): the way they face,
        or where the head points when lying. posture: standing | sitting | lying | absent."""
        name = name.lower()
        if name not in self.geometry.people_geoms:
            raise KeyError(f"no resident {name!r} in the {self.variant!r} variant (has: {sorted(self.geometry.people_geoms)})")
        if posture not in POSTURES:
            raise ValueError(f"posture must be one of {POSTURES}")
        x, y, yaw = float(x), float(y), float(yaw)
        if not all(math.isfinite(v) for v in (x, y, yaw)) or abs(x) > 10 or abs(y) > 10:
            raise ValueError("person pose out of range")
        with self._lock:
            self.geometry.pose_person(name, posture, x, y, yaw)
            self.geometry.refresh_people()
            self.people[name] = {"posture": posture, "x": x, "y": y, "yaw_deg": yaw}

    def _run_scenario(self):
        if callable(self.scenario):
            self.scenario(self, self.t)
        elif self.scenario:
            for i, (t_s, name, posture, x, y, yaw) in enumerate(self.scenario):
                if i not in self._events_done and self.t >= t_s:
                    self._events_done.add(i)
                    with contextlib.suppress(KeyError, ValueError):
                        self.set_person_pose(name, x, y, yaw, posture)

    def _poll_control(self):
        """Live commands appended to the control file, one JSON object per line:
        {"person": "jeanine", "posture": "lying", "x": -1.3, "y": 0.1, "yaw_deg": 8} | {"teleport": [x, y, yaw_deg]} | {"battery": 35}"""
        try:
            with self.control_file.open("r", encoding="utf-8") as fh:
                fh.seek(self._control_offset)
                lines = fh.readlines()
                self._control_offset = fh.tell()
        except OSError:
            return
        for line in lines[-20:]:
            with contextlib.suppress(Exception):  # a malformed or impossible command is ignored, never fatal
                cmd = json.loads(line)
                if "person" in cmd:
                    now = self.people.get(str(cmd["person"]).lower(), {})
                    self.set_person_pose(cmd["person"], cmd.get("x", now.get("x", 0.0)), cmd.get("y", now.get("y", 0.0)),
                                         cmd.get("yaw_deg", now.get("yaw_deg", 0.0)), cmd.get("posture", now.get("posture", "standing")))
                elif "teleport" in cmd:
                    self.teleport(*[float(v) for v in cmd["teleport"]][:3])
                elif "battery" in cmd:
                    self.set_battery(cmd["battery"])

    # ------------------------------------------------------------------ sport API
    def _handle_request(self, topic, options):
        api = options.get("api_id")
        if topic == TOPIC_MOTION_SWITCHER:
            return _response(CODE_OK, {"name": "mcf"} if api == 1001 else None)
        if topic != TOPIC_SPORT:
            return _response(CODE_OK)  # obstacle-avoid switch, vui, audio hub: accepted, no effect
        if api == STOP_MOVE:
            self._cmd = (np.zeros(3), -1e9)
            self.vel = np.zeros(3)
            return _response(CODE_OK)
        if api == MOVE:
            self._on_move(options.get("parameter"))
            return _response(CODE_OK)
        if api == EULER:
            with contextlib.suppress(Exception):
                self.pitch = max(-0.75, min(0.75, float(json.loads(options.get("parameter") or "{}").get("y", 0.0))))
            return _response(CODE_OK)
        if api in REFUSED_APIS or api not in SPORT_ACTIONS:
            return _response(CODE_NOT_IMPLEMENTED)
        if self.busy and api not in ALWAYS_ACKED:
            return _response(CODE_BUSY)  # the firmware answers -1 while it finishes the previous motion
        if self.busy and api == 1002:
            return _response(CODE_OK)    # acked, but it does not cut the running trick short
        name, busy_s, posture = SPORT_ACTIONS[api]
        self.busy_until, self.action = self.t + busy_s, name
        if posture:
            self.posture = posture
        if api == 1002:
            self.pitch = 0.0
        self._cmd = (np.zeros(3), -1e9)
        return _response(CODE_OK)

    # ------------------------------------------------------------------ telemetry
    def pose_message(self):
        qx, qy, qz, qw = yaw_quaternion(self.yaw)
        return {"type": "msg", "topic": TOPIC_POSE, "data": {
            "header": {"frame_id": "odom", "stamp": {"sec": int(self.t), "nanosec": int((self.t % 1) * 1e9)}},
            "pose": {"position": {"x": self.x, "y": self.y, "z": POSE_Z[self.posture]},
                     "orientation": {"x": qx, "y": qy, "z": qz, "w": qw}}}}

    def lowstate_message(self):
        return {"type": "msg", "topic": TOPIC_LOWSTATE, "data": {
            "bms_state": {"soc": int(round(self.soc)), "current": -2400 if self.vel.any() else -900},
            "power_v": round(25.0 + 4.0 * self.soc / 100.0, 2), "imu_state": {"rpy": [0.0, self.pitch, self.yaw]}}}

    def sport_state_message(self):
        return {"type": "msg", "topic": TOPIC_SPORT_STATE, "data": {
            "mode": 1 if self.posture == "stand" else 5, "progress": 1 if self.busy else 0, "gait_type": 1,
            "body_height": POSE_Z[self.posture], "position": [self.x, self.y, POSE_Z[self.posture]],
            "velocity": [float(self.vel[0]), float(self.vel[1]), float(self.vel[2])]}}

    def voxel_message(self):
        t0 = time.perf_counter()
        with self._lock:
            data = self.geometry.voxel_data(self.x, self.y, self.t)
        self.stats["voxel_ms"], self.stats["voxel_cells"] = (time.perf_counter() - t0) * 1000, data["data"]["face_count"]
        return {"type": "msg", "topic": TOPIC_VOXELS, "data": data}

    def messages(self):
        """One tick of telemetry for the subscribed topics, pose before the voxel map that is placed with it."""
        return [(topic, build()) for topic, build in ((TOPIC_LOWSTATE, self.lowstate_message), (TOPIC_POSE, self.pose_message),
                                                      (TOPIC_SPORT_STATE, self.sport_state_message), (TOPIC_VOXELS, self.voxel_message))
                if topic in self.subs]

    def _deliver(self, messages):
        if self._closed:
            return
        for topic, message in messages:
            callback = self.subs.get(topic)
            if callback is not None:
                callback(message)

    def publish(self):
        """Deliver one tick on the calling thread (tests that drive the dog without the clock)."""
        self._deliver(self.messages())

    # ------------------------------------------------------------------ camera
    def _attitude(self):
        """(z, pitch, roll) of the rendered base: trot wobble while walking, cosmetic attitude during tricks."""
        phase = self.travelled / 0.22 * math.pi
        speed = min(1.0, float(np.hypot(self.vel[0], self.vel[1])) / 0.2)
        z = RENDER_BASE_Z - (POSE_Z["stand"] - POSE_Z[self.posture]) + 0.004 * speed * math.sin(2 * phase)
        pitch = self.pitch + math.radians(0.5) * speed * math.sin(2 * phase)
        if self.posture == "sit":
            pitch += TRICK_PITCH["Sit"]
        if self.busy and self.action in TRICK_PITCH and self.action != "Sit":
            pitch += TRICK_PITCH[self.action]
        if self.busy and self.action in ("Dance1", "Dance2", "WiggleHips"):
            pitch += 0.10 * math.sin(self.t * 5.0)
        return z, pitch, math.radians(0.7) * speed * math.sin(phase), phase

    def _write_qpos(self, data, key_id):
        import mujoco
        mujoco.mj_resetDataKeyframe(self.model, data, key_id)
        z, pitch, roll, phase = self._attitude()
        qx, qy, qz, qw = yaw_quaternion(self.yaw, pitch, roll)
        data.qpos[0:3], data.qpos[3:7] = (self.x, self.y, z), (qw, qx, qy, qz)
        if np.hypot(self.vel[0], self.vel[1]) > 0.03:
            for leg, offset in enumerate((0.0, math.pi, math.pi, 0.0)):  # FL, FR, RL, RR: diagonal pairs in phase
                data.qpos[7 + 3 * leg + 1] = 0.9 + 0.22 * math.sin(phase + offset)
                data.qpos[7 + 3 * leg + 2] = -1.8 - 0.28 * max(0.0, math.cos(phase + offset))
        mujoco.mj_forward(self.model, data)

    def _publish_frame(self, bgr):
        self._frame_seq += 1
        self._frame = SimFrame(bgr, self._frame_seq, self.t)
        self.stats["frames"] += 1

    def _render(self, renderer, data, key_id):
        with self._lock:
            self._write_qpos(data, key_id)
            renderer.update_scene(data, camera=apartment.CAMERA["name"])
        return np.ascontiguousarray(renderer.render()[:, :, ::-1])

    def _render_loop(self):
        """Owns the GL context (created and used on this thread only)."""
        import mujoco
        renderer = mujoco.Renderer(self.model, height=RENDER_H, width=RENDER_W)
        data = mujoco.MjData(self.model)
        key_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "apartment_start")
        period = 1.0 / self.camera_hz
        try:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                bgr = self._render(renderer, data, key_id)
                took = time.perf_counter() - t0
                self.stats["render_ms"] = took * 1000 if self.stats["render_ms"] is None else 0.9 * self.stats["render_ms"] + 100 * took
                self._publish_frame(bgr)
                self._stop.wait(max(0.0, period - took))
        finally:
            renderer.close()

    def render_frame(self):
        """One camera frame rendered on the calling thread (tests, snapshots); the live loop is not needed."""
        import mujoco
        renderer = mujoco.Renderer(self.model, height=RENDER_H, width=RENDER_W)
        try:
            data = mujoco.MjData(self.model)
            key_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "apartment_start")
            return SimFrame(self._render(renderer, data, key_id), self._frame_seq + 1, self.t)
        finally:
            renderer.close()

    def snapshot(self):
        return {"variant": self.variant, "t_s": round(self.t, 2), "pose": {"x": round(self.x, 3), "y": round(self.y, 3),
                "yaw_deg": round(math.degrees(self.yaw), 1)}, "posture": self.posture, "action": self.action if self.busy else None,
                "soc": round(self.soc, 1), "contacts": self.contacts, "travelled_m": round(self.travelled, 2),
                "people": {k: dict(v) for k, v in self.people.items()}, "stats": dict(self.stats)}
