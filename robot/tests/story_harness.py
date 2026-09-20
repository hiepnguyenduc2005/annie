"""Fake world + virtual-time event loop for Monte-Carlo runs of the demo stories. No SDK, hardware or network.

`test_go2_patrol_greet_runtime.py` drives `run_patrol_greet` with a one-dimensional fake dog in real time, which
costs 3+ s per run (the stand-up sleeps) and cannot turn. This harness keeps the same seams (conn_factory, tracker,
encoder, speak, view.missions) and adds what the stories need:

* `VirtualLoop`: an asyncio loop whose clock is a number. It jumps straight to the next timer, so production's
  hard-coded waits (1.5 s stand-up, 4 s trick settle, 25 s named-person window, 60 s mission hold) cost nothing and
  production parameters (15 Hz control, 1 s telemetry staleness, the default planner / stall detector / bandit) run
  unchanged. The clock is frozen while production's worker threads hold work (a camera frame in the perception
  pipeline, a voxel map in the voxel worker, an `instruct` being planned, a `say` / `listen` in the executor), so
  a seed replays tick for tick: same trajectory, same report. Every time below is in virtual seconds.
* `World`: a rectangular room in the odometry frame with optional furniture blocks and people. The dog integrates
  the streamed Move (vx, wz) with a first-order lag and the Go2's measured deadbands (`smart_patrol.MIN_WALK_MPS`,
  `MIN_TURN_RPS`): a turn in place slower than the deadband does not turn. Walls, blocks and people are solid: the
  nose (0.35 m ahead of the pose) cannot enter them, which is what the odometry stall detector has to notice.
* `SimTracker`: the camera, 100 ms late. It projects people with patrol.py's own model (100 degree horizontal
  view, box height fraction = 0.55 / distance) plus per-frame jitter, missed frames, a flickering shirt-colour
  identity, ByteTrack-like id loss after two seconds out of view, and optional id churn.
* `SimDog`: the WebRTC connection. Publishes lowstate + pose at 25 Hz and a voxel map (floor patch, walls, blocks,
  people, each voxel as four face vertices like the driver's decode) at 1.5-4 Hz with +-1 cell jitter and an
  optional stale-map gap. Records every sport request and Move with its time.

Instrumentation, not behaviour: while a story runs, `patrol.Perception` and `patrol.Diag` are subclasses that
count submitted frames / processed voxel maps so the loop knows when the threads are idle; the host speaker,
microphone and every model client are replaced (`hermetic`), which is the demo's no-model fallback.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import math
import os
import random
import selectors
import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
import robot.dog.inference as inference_mod  # noqa: E402
import robot.dog.runtime.patrol as patrol  # noqa: E402
from robot.dog.missions import errand as errand_mod  # noqa: E402
from robot.dog.planning.smart_patrol import MIN_TURN_RPS, MIN_WALK_MPS, wrap_angle  # noqa: E402

FRAME_W, FRAME_H = 640, 480
HFOV = math.radians(100.0)  # patrol.py places people with the same figure
BOX_K = 0.55                # box height fraction = BOX_K * size / distance: the inverse of patrol.py's `0.55 / hfrac`
CAMERA_LATENCY_S = 0.10     # capture -> tracks; the live pipeline measures 70-120 ms
TRACK_BUFFER_S = 2.0        # ByteTrack keeps a lost track ~30 frames; out of view longer than this = a new track id
NOSE_M = 0.35               # pose -> nose / tail of the Go2
PERSON_R = 0.15             # solid radius of a standing person (shins); a lying body is a capsule of the same radius
LYING_LEN_M = 1.6
RES = 0.05                  # voxel size
STUCK_REAL_S = 1.0          # a worker thread silent this long (real time) no longer holds the clock; counted in `forced`
LOWSTATE = {"data": {"bms_state": {"soc": 55, "current": -2481}, "power_v": 28.3, "imu_state": {"rpy": [0, 0, 3.0]}}}
AUTHOR = "Henry"


# ---------------------------------------------------------------------------------------------------------------
# virtual time
# ---------------------------------------------------------------------------------------------------------------
class _VirtualSelector(selectors.DefaultSelector):
    loop = None

    def select(self, timeout=None):
        loop = self.loop
        if timeout is not None and timeout <= 0:
            return super().select(0)
        t_real = time.monotonic()
        while loop.is_busy():  # a thread holds work for this instant: wait for it in real time, clock frozen
            events = super().select(0.0002)
            if events:
                return events
            if time.monotonic() - t_real > STUCK_REAL_S:
                loop.forced += 1
                for probe in loop.probes:
                    getattr(probe, "resync", lambda: None)()
                break
        if timeout is None:
            return super().select(0.02)
        events = super().select(0)
        if events:
            return events
        loop.now += timeout  # nothing to wait for: jump to the next timer
        return []


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        selector = _VirtualSelector()
        selector.loop = self
        super().__init__(selector)
        self.now, self.jobs, self.sleepers, self.holding, self.forced, self.probes = 0.0, 0, 0, 0, 0, []

    def time(self):
        return self.now

    def is_busy(self):
        return self.holding > 0 or self.jobs > self.sleepers or any(p.busy() for p in self.probes)

    def run_in_executor(self, executor, func, *args):
        future = super().run_in_executor(executor, func, *args)
        self.jobs += 1
        future.add_done_callback(lambda _f: setattr(self, "jobs", self.jobs - 1))
        return future

    async def shutdown_default_executor(self, timeout=None):
        self.holding += 1  # joins threads in real time; the 300 s guard must not fire on the virtual clock
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self.holding -= 1

    def sleep_in_thread(self, seconds):
        """Called from an executor job: block the thread for `seconds` of virtual time."""
        woke = threading.Event()

        def arm():
            self.sleepers += 1
            self.call_later(seconds, lambda: (setattr(self, "sleepers", self.sleepers - 1), woke.set()))
        self.call_soon_threadsafe(arm)
        woke.wait(20.0)


class _InstructProbe:
    @staticmethod
    def busy():
        return any(t.name == "instruct" for t in threading.enumerate())


# ---------------------------------------------------------------------------------------------------------------
# world
# ---------------------------------------------------------------------------------------------------------------
class Person:
    def __init__(self, *, x, y, name=None, size=1.0, posture="upright", appear_t=0.0, away=(), id_prob=1.0,
                 lying_axis=0.0, churn_s=None, posture_flicker=0.0):
        self.x, self.y, self.name, self.size, self.posture = x, y, name, size, posture
        self.appear_t, self.away, self.id_prob = appear_t, tuple(away), id_prob
        self.lying_axis, self.churn_s, self.posture_flicker = lying_axis, churn_s, posture_flicker
        self.tid, self.first_seen, self.last_seen, self.lying_frames, self.tids = None, None, None, 0, []

    def present(self, now) -> bool:
        return self.x is not None and now >= self.appear_t and not any(a <= now < b for a, b in self.away)

    def segment(self):
        """The solid body: a point for a standing person, a 1.6 m segment for a lying one."""
        if self.posture != "lying":
            return (self.x, self.y), (self.x, self.y)
        hx, hy = 0.5 * LYING_LEN_M * math.cos(self.lying_axis), 0.5 * LYING_LEN_M * math.sin(self.lying_axis)
        return (self.x - hx, self.y - hy), (self.x + hx, self.y + hy)

    def gap_to(self, px, py) -> float:
        (ax, ay), (bx, by) = self.segment()
        dx, dy = bx - ax, by - ay
        u = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


class World:
    """Room bounds are wall faces. `blocks` are (x0, y0, x1, y1) furniture the LiDAR sees and the dog cannot enter."""

    def __init__(self, *, clock, rng, room=(-2.0, -2.5, 3.5, 2.5), blocks=(), people=(), yaw0=0.0):
        self.clock, self.rng, self.room, self.blocks, self.people = clock, rng, room, list(blocks), list(people)
        self.pose = (0.0, 0.0, yaw0)
        self.history = collections.deque([(0.0, self.pose)], maxlen=16)
        self.cmd, self.vx, self.wz = (0.0, 0.0), 0.0, 0.0
        self.bumps: list[dict] = []
        self._blocked = None
        self.min_person_gap = float("inf")
        self.path_m = 0.0

    def solid_at(self, px, py, now):
        x0, y0, x1, y1 = self.room
        if not (x0 < px < x1 and y0 < py < y1):
            return "wall"
        for bx0, by0, bx1, by1 in self.blocks:
            if bx0 <= px <= bx1 and by0 <= py <= by1:
                return "block"
        for p in self.people:
            if p.present(now) and p.gap_to(px, py) < PERSON_R:
                return "person"
        return None

    def pose_at(self, t):
        for when, pose in reversed(self.history):
            if when <= t:
                return pose
        return self.history[0][1]

    def step(self, dt, now):
        x, y, yaw = self.pose
        cvx, cwz = self.cmd
        walking = abs(cvx) >= MIN_WALK_MPS - 0.02
        turning = walking or abs(cwz) >= MIN_TURN_RPS - 0.05  # a slow turn in place does not move the legs
        a = min(1.0, dt / 0.25)
        self.vx += ((cvx if walking else 0.0) - self.vx) * a
        self.wz += ((cwz if turning else 0.0) - self.wz) * a
        yaw = wrap_angle(yaw + self.wz * dt)
        d = self.vx * dt
        if abs(d) > 1e-6:
            c, s = math.cos(yaw), math.sin(yaw)
            nx, ny = x + d * c, y + d * s
            probe = NOSE_M if d > 0 else -NOSE_M
            hit = self.solid_at(nx + probe * c, ny + probe * s, now)
            if hit:
                self.vx = 0.0
                if self._blocked != hit:
                    self.bumps.append({"t_s": round(now, 1), "what": hit, "cmd_vx": round(cvx, 2)})
                self._blocked = hit
            else:
                self.path_m += abs(d)
                x, y, self._blocked = nx, ny, None
        self.pose = (x, y, yaw)
        self.history.append((now, self.pose))
        c, s = math.cos(yaw), math.sin(yaw)
        for p in self.people:
            if p.present(now):
                self.min_person_gap = min(self.min_person_gap, p.gap_to(x + NOSE_M * c, y + NOSE_M * s) - PERSON_R)


# ---------------------------------------------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------------------------------------------
class SimTracker:
    """`update(img, now_ms)` as `robot.simulation.person_tracker.PersonTracker` answers it, from the world's truth."""

    def __init__(self, world: World, *, seed, miss_prob=0.03, jitter=0.02):
        self.world, self.rng, self.miss_prob, self.jitter = world, random.Random(seed), miss_prob, jitter
        self.next_tid, self.updates = 1, 0

    def update(self, img, now_ms):
        self.updates += 1
        now = self.world.clock() - CAMERA_LATENCY_S
        x, y, yaw = self.world.pose_at(now)
        out = []
        for p in self.world.people:
            if not p.present(now):
                continue
            dist = math.hypot(p.x - x, p.y - y)
            bearing = wrap_angle(math.atan2(p.y - y, p.x - x) - yaw)
            if not 0.3 <= dist <= 6.0 or abs(bearing) >= HFOV / 2:
                continue
            full = min(1.0, BOX_K * p.size / dist) * FRAME_H
            if p.posture == "lying":  # wide and short, low in the frame
                h, w, cy = 0.3 * full, min(FRAME_W * 0.9, 1.5 * full), FRAME_H * 0.62
            else:
                h, w, cy = full, 0.36 * full, FRAME_H * 0.5
            j = self.jitter
            h, w = h * (1 + self.rng.uniform(-j, j)), w * (1 + self.rng.uniform(-j, j))
            cx = (0.5 - bearing / HFOV) * FRAME_W + self.rng.uniform(-j, j) * w
            x1, x2 = max(0.0, cx - w / 2), min(float(FRAME_W), cx + w / 2)
            if x2 - x1 < 0.4 * w:
                continue  # mostly out of frame: no detection
            y1, y2 = max(0.0, cy - h / 2), min(float(FRAME_H), cy + h / 2)
            if p.last_seen is None or now - p.last_seen > TRACK_BUFFER_S or \
                    (p.churn_s and p.first_seen is not None and now - p.first_seen >= p.churn_s):
                p.tid, p.first_seen, p.lying_frames = self.next_tid, now, 0  # the tracker lost them: a new id
                p.tids.append(p.tid)
                self.next_tid += 1
            p.last_seen = now
            if self.rng.random() < self.miss_prob:
                continue
            posture = p.posture
            if p.posture_flicker and self.rng.random() < p.posture_flicker:
                posture = "unknown"
            p.lying_frames = p.lying_frames + 1 if posture == "lying" else 0
            track = {"track_id": p.tid, "box": [x1, y1, x2, y2], "conf": round(self.rng.uniform(0.6, 0.92), 2),
                     "posture": posture, "lying_frames": p.lying_frames, "kp_conf": [0.9] * 17,
                     "first_seen_ms": now_ms - int((now - p.first_seen) * 1000)}
            if p.name and self.rng.random() < p.id_prob:
                track["identity"] = {"name": p.name, "score": 0.8, "method": "shirt_colour"}
            out.append(track)
        return out


# ---------------------------------------------------------------------------------------------------------------
# robot link
# ---------------------------------------------------------------------------------------------------------------
class _Track:
    def __init__(self, hz):
        self.period = 1.0 / hz

    async def recv(self):
        await asyncio.sleep(self.period)
        return object()


class _Video:
    def __init__(self, hz):
        self.hz = hz

    def add_track_callback(self, cb):
        asyncio.ensure_future(cb(_Track(self.hz)))


class _PubSub:
    def __init__(self, dog):
        self.dog = dog

    def subscribe(self, topic, callback):
        self.dog.subs[topic] = callback

    async def publish_request_new(self, topic, options):
        dog = self.dog
        dog.requests.append((round(dog.world.clock(), 2), topic, dict(options)))
        if topic == patrol.TOPIC_SPORT and options.get("api_id") == patrol.STOP_MOVE:
            dog.world.cmd = (0.0, 0.0)
        return {"data": {"header": {"status": {"code": 0}}}}

    def publish_without_callback(self, topic, data=None, msg_type=None):
        if topic == patrol.TOPIC_SPORT and isinstance(data, dict):
            p = json.loads(data["parameter"])
            self.dog.world.cmd = (float(p["x"]), float(p["z"]))
            self.dog.moves.append((round(self.dog.world.clock(), 2), float(p["x"]), float(p["z"])))


class _DataChannel:
    def __init__(self, dog):
        self.pub_sub = _PubSub(dog)

    async def disableTrafficSaving(self, switch):
        pass

    def switchVideoChannel(self, switch):
        pass


class SyntheticLabels:
    """Custom identifier seam: keep the SimTracker's synthetic labels on the tracks.

    The harness encoder hands the pipeline blank frames, so the real reid identifier
    correctly finds no shirt in the pixels and would strip these labels. A production
    deployment passes an explicit custom identifier the same way; blank-frame evidence
    is a property of the harness, not of the policy under test.
    """

    name = None  # no shirt rule: patrol only reads this to bias the explore bandit

    @staticmethod
    def apply(img, tracks):
        return tracks


class SimDog:
    def __init__(self, world: World, *, pose_hz=25.0, voxel_hz=2.5, frame_hz=14.0, stale_map=None, range_jitter=True):
        self.world, self.pose_hz, self.voxel_hz, self.stale_map, self.range_jitter = world, pose_hz, voxel_hz, stale_map, range_jitter
        self.subs, self.requests, self.moves, self.tasks, self.voxels_sent, self.pose_sent = {}, [], [], [], 0, False
        self.datachannel, self.video, self.disconnected = _DataChannel(self), _Video(frame_hz), False
        x0, y0, x1, y1 = world.room
        self.origin = (x0 - 0.5, y0 - 0.5, 0.0)
        cells = []
        for gx in range(self._i(x0, 0), self._i(x1, 0) + 1):
            cells += [(gx, self._i(y0, 1) - 1, 0), (gx, self._i(y1, 1), 0)]
        for gy in range(self._i(y0, 1), self._i(y1, 1) + 1):
            cells += [(self._i(x0, 0) - 1, gy, 0), (self._i(x1, 0), gy, 0)]
        self.walls = self._columns(cells, (3, 5, 7, 9, 11, 13))
        bcells = []
        for bx0, by0, bx1, by1 in world.blocks:
            bcells += [(gx, gy, 0) for gx in range(self._i(bx0, 0), self._i(bx1, 0) + 1)
                       for gy in range(self._i(by0, 1), self._i(by1, 1) + 1)]
        self.block_cells = self._columns(bcells, (3, 5, 7, 9))
        g = np.arange(-30, 31, 2)
        self.floor = np.stack([np.repeat(g, g.size), np.tile(g, g.size), np.zeros(g.size * g.size, dtype=int)], axis=1)

    def _i(self, v, axis):
        return int(math.floor((v - self.origin[axis]) / RES))

    @staticmethod
    def _columns(cells, zs):
        if not cells:
            return np.zeros((0, 3), dtype=int)
        base = np.asarray(cells, dtype=int)
        return np.concatenate([base + np.array([0, 0, z]) for z in zs])

    def voxel_msg(self, now):
        w, rng = self.world, self.world.rng
        x, y, _ = w.pose
        jit = (lambda: np.array([rng.randint(-1, 1), rng.randint(-1, 1), 0])) if self.range_jitter else (lambda: 0)
        parts = [self.floor + np.array([self._i(x, 0), self._i(y, 1), 0]), self.walls + jit()]
        if len(self.block_cells):
            parts.append(self.block_cells + jit())
        for p in w.people:
            if not p.present(now):
                continue
            if p.posture == "lying":
                (ax, ay), (bx, by) = p.segment()
                body = [(self._i(ax + (bx - ax) * u / 32 + ox, 0), self._i(ay + (by - ay) * u / 32 + oy, 1), 0)
                        for u in range(33) for ox in (-0.05, 0.05) for oy in (-0.05, 0.05)]
                parts.append(self._columns(sorted(set(body)), (3, 4, 5)) + jit())
            else:
                legs = [(self._i(p.x, 0) + dx, self._i(p.y, 1) + dy, 0) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)]
                parts.append(self._columns(legs, (3, 5, 7, 9, 11, 13)) + jit())
        cells = np.clip(np.concatenate(parts), 0, 255).astype(np.uint8)
        positions = np.repeat(cells, 4, axis=0).reshape(-1)  # four face vertices per voxel, as the driver decodes them
        return {"data": {"origin": list(self.origin), "resolution": RES, "data": {"positions": positions}}}

    async def connect(self):
        self.tasks = [asyncio.ensure_future(self._telemetry()), asyncio.ensure_future(self._voxels())]

    async def _telemetry(self):
        loop, last = asyncio.get_running_loop(), None
        while True:
            await asyncio.sleep(1.0 / self.pose_hz)
            now = loop.time()
            self.world.step(min(0.2, now - last) if last is not None else 0.0, now)
            last = now
            x, y, yaw = self.world.pose
            if "rt/utlidar/robot_pose" not in self.subs:
                continue
            self.pose_sent = True
            self.subs["rt/lf/lowstate"](LOWSTATE)
            self.subs["rt/utlidar/robot_pose"]({"data": {"header": {"frame_id": "odom"}, "pose": {
                "position": {"x": x, "y": y, "z": 0.32},
                "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}}}})

    async def _voxels(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(1.0 / self.voxel_hz)
            now = loop.time()
            if self.stale_map and self.stale_map[0] <= now < self.stale_map[1]:
                continue
            if patrol.TOPIC_VOXELS in self.subs and self.pose_sent and not self.disconnected:
                self.voxels_sent += 1
                self.subs[patrol.TOPIC_VOXELS](self.voxel_msg(now))

    async def disconnect(self):
        self.disconnected = True
        for t in self.tasks:
            t.cancel()


# ---------------------------------------------------------------------------------------------------------------
# hermetic seams: no speaker, no microphone, no model server
# ---------------------------------------------------------------------------------------------------------------
class _ProbedPerception(patrol.Perception):
    """The production pipeline, plus a count of submitted frames so the loop can tell when it is idle."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submitted, self.started, self.offset = 0, False, 0
        asyncio.get_running_loop().probes.append(self)

    def start(self):
        self.started = True
        return super().start()

    def submit(self, frame, t_recv=None):
        self.submitted += 1
        super().submit(frame, t_recv)

    def _in_flight(self):
        counts = self.diag.counts
        return self.submitted - self.result["seq"] - counts["dropped"] - counts["errors"] - self.offset

    def busy(self):
        return self.started and not self.stopped and self._in_flight() > 0

    def resync(self):
        self.offset += max(0, self._in_flight())


class _ProbedDiag(patrol.Diag):
    """Production counters, plus how many voxel maps the worker has finished (`voxel_ms` is sampled once per map)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voxel_maps_done = 0
        _DIAGS.append(self)

    def sample(self, name, value_ms):
        super().sample(name, value_ms)
        if name == "voxel_ms":
            self.voxel_maps_done += 1


_DIAGS: list = []


class _VoxelProbe:
    def __init__(self, dog):
        self.dog, self.offset = dog, 0

    def _in_flight(self):
        return self.dog.voxels_sent - (_DIAGS[-1].voxel_maps_done if _DIAGS else 0) - self.offset

    def busy(self):
        return not self.dog.disconnected and self._in_flight() > 0

    def resync(self):
        self.offset += max(0, self._in_flight())


@contextlib.contextmanager
def hermetic(said: list, heard: dict, dog):
    """Replace the host speaker / microphone used by `say` / `listen` missions and refuse every model client, so
    the rules fallback (`inference=None`) is what runs; install the idle probes. Restores everything afterwards."""
    loop = asyncio.get_running_loop()

    def speak_blocking(text):
        said.append((round(loop.time(), 1), text))
        loop.sleep_in_thread(min(8.0, 0.065 * len(text)))
        return True

    def listen_blocking(max_s):
        loop.sleep_in_thread(min(max_s, heard.get("after_s", 2.0)))
        return dict(heard["reply"])

    def no_model(*a, **k):
        raise RuntimeError("no model in the story harness")
    plan_instruction = patrol.agent_mod.plan_instruction

    def plan_after_the_tick(*args, **kwargs):
        """The planning thread races the control tick that started it (ready in the same tick or the next one).
        Here it always loses, as it nearly always does live: it starts once the event loop has run again."""
        ran = threading.Event()
        loop.call_soon_threadsafe(ran.set)
        ran.wait(5.0)
        return plan_instruction(*args, **kwargs)
    saved = (patrol.speak_blocking, patrol.listen_blocking, patrol.Perception, patrol.Diag, inference_mod.Inference, inference_mod.shared)
    patrol.speak_blocking, patrol.listen_blocking = speak_blocking, listen_blocking
    patrol.agent_mod.plan_instruction = plan_after_the_tick
    patrol.Perception, patrol.Diag = _ProbedPerception, _ProbedDiag
    inference_mod.Inference = inference_mod.shared = no_model
    del _DIAGS[:]
    loop.probes += [_VoxelProbe(dog), _InstructProbe]
    try:
        yield
    finally:
        patrol.agent_mod.plan_instruction = plan_instruction
        (patrol.speak_blocking, patrol.listen_blocking, patrol.Perception, patrol.Diag, inference_mod.Inference,
         inference_mod.shared) = saved


async def run_patrol(world, dog, tracker, director, *, duration_s, stop_on_checkin=False):
    """Production values as `main()` passes them (0.35 m/s, 5 m boundary, 15 Hz, 1 s telemetry staleness, 2 s LiDAR
    staleness, no idle trick) with the default planner, stall detector and heading bandit. The voxel worker's
    real-time rate limit is off: the fake link already sends maps at the robot's 1.5-4 Hz."""
    view = patrol.LiveView(port=0)
    spoken, log = [], []
    clock = asyncio.get_running_loop().time
    task = asyncio.create_task(patrol.run_patrol_greet(
        ip="10.0.0.99", aes_key=None, conn_factory=lambda ip, key: dog, tracker=tracker,
        encoder=lambda frame: (b"", FRAME_W, FRAME_H), speak=lambda text: spoken.append((round(clock(), 1), text)),
        status=lambda text: log.append(f"[{clock():6.1f}] {text}"), duration_s=duration_s, speed_mps=0.35, boundary_m=5.0,
        rate_hz=15.0, stale_s=1.0, lidar_stale_s=2.0, voxel_min_interval_s=0.0, idle_trick_s=0.0, diag_every_s=1e9,
        stop_on_checkin=stop_on_checkin, view=view, frontier_planner=None,
        source="simulation", identifier=SyntheticLabels()))
    while getattr(view, "missions", None) is None and not task.done():  # the board exists once the run has set itself up
        await asyncio.sleep(0.01)
    extra = await director(view, task)
    report = await task
    return report, spoken, log, extra


def _loop_start(log):
    """Virtual time at which the patrol loop started: report times (`t_s`, the `t=` in status lines) count from there."""
    offsets = []
    for line in log:
        head, _, rest = line.partition("] t=")
        if rest and rest.split("s", 1)[0].strip().replace(".", "", 1).isdigit():
            offsets.append(float(head[1:]) - float(rest.split("s", 1)[0]))
    return statistics.median(offsets) if offsets else 0.0


# ---------------------------------------------------------------------------------------------------------------
# Story A: "tell Grandma to plug in her phone"
# ---------------------------------------------------------------------------------------------------------------
RELAY_TEXTS = ("tell Grandma to plug in her phone", "Please tell grandma to plug in her phone.",
               "remind Grandma to plug in her phone", "Tell Jeanine to plug in her phone")


def story_a_params(seed: int) -> dict:
    rng = random.Random(1000 + seed)
    bearing = rng.uniform(-180.0, 180.0)
    p = {"seed": seed, "path": "errand" if seed % 2 == 0 else "instruct", "text": RELAY_TEXTS[seed % len(RELAY_TEXTS)],
         "dispatch_t": round(rng.uniform(4.0, 7.0), 2), "appear_after_s": round(rng.uniform(0.0, 8.0), 2),
         "bearing_deg": round(bearing, 1), "distance_m": round(rng.uniform(1.4, 3.2), 2), "size": round(rng.uniform(0.75, 1.2), 2),
         "id_prob": round(rng.uniform(0.75, 1.0), 2), "yaw0_deg": round(rng.uniform(-180, 180), 1),
         "stranger_first": rng.random() < 0.4, "leaves": rng.random() < 0.35, "stale_map": rng.random() < 0.4,
         "wall_in_front": rng.random() < 0.35, "voxel_hz": round(rng.uniform(1.5, 4.0), 1), "heard": rng.random() < 0.7}
    p["leave_after_s"], p["leave_for_s"] = round(rng.uniform(1.0, 5.0), 1), round(rng.uniform(3.0, 8.0), 1)
    p["stale_at_s"], p["stale_for_s"] = round(rng.uniform(4.0, 16.0), 1), round(rng.uniform(2.5, 6.0), 1)
    p["wall_m"] = round(rng.uniform(0.5, 0.9), 2)
    p["stranger_bearing_deg"], p["stranger_m"] = round(rng.uniform(-40, 40), 1), round(rng.uniform(1.6, 3.0), 2)
    return p


def _place(room, x, y, yaw, bearing_deg, dist, margin=0.55):
    """A point `dist` metres away at `bearing_deg` from the dog's nose, pulled inside the room."""
    ang = yaw + math.radians(bearing_deg)
    x0, y0, x1, y1 = room
    return (min(x1 - margin, max(x0 + margin, x + dist * math.cos(ang))),
            min(y1 - margin, max(y0 + margin, y + dist * math.sin(ang))))


def run_story_a(seed: int, overrides: dict | None = None) -> dict:
    p = {**story_a_params(seed), **(overrides or {})}
    return asyncio.run(_story_a(p), loop_factory=VirtualLoop)


async def _story_a(p):
    loop = asyncio.get_running_loop()
    rng = random.Random(2000 + p["seed"])
    yaw0 = math.radians(p["yaw0_deg"])
    blocks = []
    if p["wall_in_front"]:  # a 1.2 m wide piece of furniture across the nose at start
        c, s, d = math.cos(yaw0), math.sin(yaw0), p["wall_m"] + 0.1
        cx, cy = d * c, d * s
        half = (0.1, 0.6) if abs(c) >= abs(s) else (0.6, 0.1)
        blocks.append((cx - half[0], cy - half[1], cx + half[0], cy + half[1]))
    world = World(clock=loop.time, rng=rng, blocks=blocks, yaw0=yaw0)
    jeanine = Person(x=None, y=None, name="Jeanine", size=p["size"], id_prob=p["id_prob"], appear_t=1e9)
    world.people.append(jeanine)
    stranger = None
    if p["stranger_first"]:
        sx, sy = _place(world.room, 0.0, 0.0, yaw0, p["stranger_bearing_deg"], p["stranger_m"])
        stranger = Person(x=sx, y=sy, name=None, size=1.0, appear_t=p["dispatch_t"] - 1.0)
        world.people.append(stranger)
    stale = (p["stale_at_s"], p["stale_at_s"] + p["stale_for_s"]) if p["stale_map"] else None
    dog = SimDog(world, voxel_hz=p["voxel_hz"], stale_map=stale)
    tracker = SimTracker(world, seed=3000 + p["seed"])
    said, events = [], []
    heard = {"after_s": rng.uniform(1.5, 4.0),
             "reply": {"transcript": "okay thank you", "heard": True, "speech_ms": 1200} if p["heard"]
             else {"transcript": None, "heard": False, "speech_ms": 0}}
    timeout_s = errand_mod.FIND_TIMEOUT_S if p["path"] == "errand" else 60.0
    marks = {}

    async def director(view, task):
        board = view.missions
        await asyncio.sleep(p["dispatch_t"])
        marks["dispatch"] = loop.time()

        async def jeanine_walks_in():
            await asyncio.sleep(p["appear_after_s"])
            x, y, yaw = world.pose
            for _ in range(20):  # somewhere in the room that is not in furniture, on the dog or on the other person
                jx, jy = _place(world.room, x, y, yaw, p["bearing_deg"], p["distance_m"])
                if world.solid_at(jx, jy, -1.0) is None and math.hypot(jx - x, jy - y) > 1.0 \
                        and (stranger is None or math.hypot(jx - stranger.x, jy - stranger.y) > 0.8):
                    break
                p["bearing_deg"] = round(rng.uniform(-180, 180), 1)
            jeanine.x, jeanine.y, jeanine.appear_t = jx, jy, loop.time()
            marks["appear"] = loop.time()
            if p["leaves"]:
                t0 = loop.time() + p["leave_after_s"]
                jeanine.away = ((t0, t0 + p["leave_for_s"]),)
        walk_in = asyncio.create_task(jeanine_walks_in())
        n = [0]

        async def body_command(name, args):
            """What `errand.BodyClient.command` does over HTTP, against the patrol's board: submit (retrying a
            busy board for BUSY_WAIT_S), poll the receipt every 0.5 s, enforce the client deadline."""
            if name == "stop":
                return board.submit({"name": "stop"})[1]
            n[0] += 1
            cid = f"story-a-{p['seed']}-{n[0]}"

            async def drive():
                busy_until = loop.time() + errand_mod.BUSY_WAIT_S
                while True:
                    code, receipt = board.submit({"command_id": cid, "name": name, "args": args})
                    if code != 409:
                        break
                    if loop.time() >= busy_until:
                        raise errand_mod.ErrandError("The robot body is busy with another command.")
                    await asyncio.sleep(0.5)
                if code not in (200, 202):
                    raise errand_mod.ErrandError(f"The robot body rejected {name} (HTTP {code}).")
                while receipt["state"] not in errand_mod.RECEIPT_TERMINAL:
                    if task.done():
                        raise errand_mod.ErrandError("The robot body service is unreachable.")
                    await asyncio.sleep(0.5)
                    receipt = board.get(cid)
                return receipt
            try:
                receipt = await asyncio.wait_for(drive(), errand_mod.DEADLINES_S.get(name, 15.0))
            except asyncio.TimeoutError:
                raise errand_mod.ErrandError(f"The robot body did not finish {name} in time.") from None
            marks.setdefault("receipts", []).append(receipt)
            if name == "find_person":
                marks["find_done"] = loop.time()
            if receipt["state"] != "completed":
                raise errand_mod.ErrandError(f"The robot body could not finish {name} ({receipt['state']}).")
            return receipt["result"] if isinstance(receipt["result"], dict) else {}

        async def post_event(run_id, kind, payload, at):
            return True
        outcome = None
        if p["path"] == "errand":
            errand = errand_mod.Errand(body_command, post_event, status=lambda text: None)
            outcome = await errand.run("00000000-0000-4000-8000-%012d" % p["seed"], AUTHOR, p["text"], log=events)
        else:
            cid = f"story-a-{p['seed']}-instruct"
            code, receipt = board.submit({"command_id": cid, "name": "instruct", "args": {"text": p["text"], "author": AUTHOR}})
            deadline = loop.time() + 100.0
            while code == 202 and receipt["state"] not in errand_mod.RECEIPT_TERMINAL and loop.time() < deadline and not task.done():
                await asyncio.sleep(0.5)
                receipt = board.get(cid)
                child = board.get(f"{cid}#1")
                if child and child["state"] in errand_mod.RECEIPT_TERMINAL:
                    marks.setdefault("find_done", loop.time())
            outcome = receipt.get("state") if code == 202 else f"rejected:{code}"
            marks["receipts"] = [r for r in board.recent(12)[::-1] if r.get("parent") == cid]
            marks["parent"] = receipt
        marks["done"] = loop.time()
        await asyncio.sleep(2.0)  # the dog should now be standing quietly by her
        walk_in.cancel()
        task.cancel()
        return outcome

    with hermetic(said, heard, dog):
        report, spoken, log, outcome = await run_patrol(world, dog, tracker, director, duration_s=p["dispatch_t"] + 125.0)
    t_loop = _loop_start(log)
    find = next((r for r in marks.get("receipts", []) if r["name"] == "find_person"), None)
    found = (find or {}).get("result") or {}
    # "the approach" = from the status line that says she was found until the find_person receipt was terminal
    found_line = next((ln for ln in log if "mission find_person: found track" in ln), None)
    approach = (float(found_line[1:7]), marks.get("find_done", float("inf"))) if found_line else None
    in_approach = [c for c in report["collisions"]
                   if c["mode"] == "follow" or (approach and approach[0] <= c["t_s"] + t_loop <= approach[1])]
    bumps_in_approach = [b for b in world.bumps if approach and approach[0] <= b["t_s"] <= approach[1]]
    say_texts = [t for _, t in said]
    greetings_after = [g for g in report["greetings"] if g["t_s"] + t_loop >= marks.get("dispatch", 0.0)]
    last = dog.requests[-1] if dog.requests else None
    stop_last = bool(last and last[2].get("api_id") == patrol.STOP_MOVE and last[2].get("priority") == 1)
    want_line = errand_mod.phrase_message(AUTHOR, "plug in her phone") if p["path"] == "errand" else None
    reason = "ok"
    if not str(report["reason"]) in ("operator_cancelled", "duration_complete"):
        reason = f"patrol_exit:{report['reason']}"
    elif not found.get("found"):
        reason = "not_found_in_time" if find else f"no_find_receipt:{outcome}"
    elif not found.get("matched_name") or (found.get("identity") or {}).get("name") != "Jeanine":
        reason = "walked_up_to_the_wrong_person"
    elif not found.get("approached"):
        reason = "not_approached"
    elif outcome != "completed":
        reason = f"chain_{outcome}"
    elif len(say_texts) != 1 or (want_line and say_texts[0] != want_line) or "plug in" not in say_texts[0]:
        reason = "wrong_words"
    elif not any(r["name"] == "listen" and r["state"] == "completed" for r in marks.get("receipts", [])):
        reason = "no_listen"
    elif greetings_after:
        reason = "greeted_on_top_of_the_errand"
    return {"seed": p["seed"], "params": p, "ok": reason == "ok", "reason": reason, "outcome": outcome,
            "patrol_reason": report["reason"], "completed": report["completed"], "found": bool(found.get("found")),
            "matched": bool(found.get("matched_name")), "searched_s": found.get("searched_s"), "timeout_s": timeout_s,
            "appear_to_arrive_s": None if not found.get("found") or "appear" not in marks
            else round(marks["dispatch"] + found["searched_s"] - marks["appear"], 1),
            "collisions": report["collisions"], "collisions_in_approach": in_approach, "bumps": world.bumps,
            "bumps_in_approach": bumps_in_approach, "min_person_gap_m": round(world.min_person_gap, 2),
            "stop_last": stop_last, "disconnected": dog.disconnected, "say": say_texts, "spoken": [t for _, t in spoken],
            "events": [e["kind"] for e in events], "modes": report["modes"], "frames": report["frames"], "forced": loop.forced,
            "log": log[-60:] if reason != "ok" or os.environ.get("ANNIE_STORY_LOG") else []}


# ---------------------------------------------------------------------------------------------------------------
# Story B: "someone is down"
# ---------------------------------------------------------------------------------------------------------------
def story_b_params(seed: int) -> dict:
    rng = random.Random(5000 + seed)
    return {"seed": seed, "appear_t": round(rng.uniform(4.0, 12.0), 2), "bearing_deg": round(rng.uniform(-35, 35), 1),
            "distance_m": round(rng.uniform(1.6, 3.0), 2), "size": round(rng.uniform(0.85, 1.15), 2),
            "lying_axis_deg": round(rng.uniform(0, 180), 1), "yaw0_deg": round(rng.uniform(-180, 180), 1),
            "stop_on_checkin": rng.random() < 0.4, "bystander": rng.random() < 0.35, "voxel_hz": round(rng.uniform(1.5, 4.0), 1),
            "posture_flicker": round(rng.choice((0.0, 0.0, 0.1, 0.2)), 2), "churn_s": None, "watch_s": 25.0}


def run_story_b(seed: int, overrides: dict | None = None) -> dict:
    p = {**story_b_params(seed), **(overrides or {})}
    return asyncio.run(_story_b(p), loop_factory=VirtualLoop)


async def _story_b(p):
    loop = asyncio.get_running_loop()
    rng = random.Random(6000 + p["seed"])
    world = World(clock=loop.time, rng=rng, yaw0=math.radians(p["yaw0_deg"]))
    down = Person(x=None, y=None, posture="lying", size=p["size"], appear_t=1e9, posture_flicker=p["posture_flicker"],
                  churn_s=p["churn_s"])
    world.people.append(down)
    dog = SimDog(world, voxel_hz=p["voxel_hz"])
    tracker = SimTracker(world, seed=7000 + p["seed"])
    marks = {}

    async def director(view, task):
        await asyncio.sleep(p["appear_t"])
        x0, y0, x1, y1 = world.room
        bearing, dist, axis = p["bearing_deg"], p["distance_m"], p["lying_axis_deg"]
        while not task.done():  # someone goes down in view, a fair way off: never under the dog's nose or through a wall
            x, y, yaw = world.pose
            ang = yaw + math.radians(bearing)
            down.x, down.y, down.lying_axis = x + dist * math.cos(ang), y + dist * math.sin(ang), yaw + math.radians(axis)
            ends = down.segment()
            if all(x0 + 0.3 < ex < x1 - 0.3 and y0 + 0.3 < ey < y1 - 0.3 for ex, ey in ends) and down.gap_to(x, y) >= 1.2:
                break
            down.x = None
            bearing, dist, axis = rng.uniform(-35, 35), rng.uniform(1.6, 3.0), rng.uniform(0, 180)
            await asyncio.sleep(0.25)
        p["bearing_deg"], p["distance_m"], p["lying_axis_deg"] = round(bearing, 1), round(dist, 2), round(axis, 1)
        down.appear_t = marks["appear"] = loop.time()
        if p["bystander"]:  # someone standing a little further back, off to one side
            bx, by = _place(world.room, x, y, yaw, p["bearing_deg"] + rng.choice((-30, 30)), p["distance_m"] + 0.8)
            world.people.append(Person(x=bx, y=by, appear_t=loop.time()))
        deadline = loop.time() + 40.0
        while not task.done() and loop.time() < deadline and not view.report["checkins"]:
            await asyncio.sleep(0.25)
        marks["first_checkin"] = loop.time() if view.report["checkins"] else None
        end = loop.time() + p["watch_s"]  # keep watching: the second question must not come
        while not task.done() and loop.time() < end:
            await asyncio.sleep(0.25)
        task.cancel()

    with hermetic([], {"reply": {}}, dog):
        report, spoken, log, _ = await run_patrol(world, dog, tracker, director, duration_s=p["appear_t"] + 80.0,
                                                  stop_on_checkin=p["stop_on_checkin"])
    t_loop = _loop_start(log)
    asked = [(t, s) for t, s in spoken if "are you alright" in s.lower()]
    lying_tids = set(down.tids)
    greeted_lying = [g for g in report["greetings"] if g["track_id"] in lying_tids]
    last = dog.requests[-1] if dog.requests else None
    stop_last = bool(last and last[2].get("api_id") == patrol.STOP_MOVE and last[2].get("priority") == 1)
    t_check = None if not report["checkins"] else report["checkins"][0]["t_s"] + t_loop
    moved_after = [m for m in dog.moves if t_check is not None and m[0] > t_check + 6.5 and (abs(m[1]) >= 0.18 or abs(m[2]) >= 0.75)]
    hellos = [r for r in dog.requests if r[2].get("api_id") == patrol.HELLO]
    reason = "ok"
    if len(report["checkins"]) == 0:
        reason = "no_checkin" if any("lying" in ln for ln in log) or tracker.updates else "never_seen"
    elif len(report["checkins"]) > 1 or len(asked) != 1:
        reason = f"asked_{max(len(report['checkins']), len(asked))}_times"
    elif greeted_lying:
        reason = "greeted_the_lying_person"
    elif p["stop_on_checkin"] and (report["reason"] != "checkin_raised" or not report["completed"]):
        reason = f"did_not_hand_over:{report['reason']}"
    elif not p["stop_on_checkin"] and report["reason"] not in ("operator_cancelled", "duration_complete"):
        reason = f"patrol_exit:{report['reason']}"
    elif not p["stop_on_checkin"] and not moved_after:
        reason = "froze_after_checkin"
    elif world.min_person_gap < 0.0 or any(b["what"] == "person" for b in world.bumps):
        reason = "touched_the_person"
    return {"seed": p["seed"], "params": p, "ok": reason == "ok", "reason": reason, "patrol_reason": report["reason"],
            "checkins": len(report["checkins"]), "asked": len(asked), "hellos": len(hellos), "greetings": len(report["greetings"]),
            "greeted_lying": len(greeted_lying), "stop_last": stop_last, "collisions": report["collisions"], "bumps": world.bumps,
            "min_person_gap_m": round(world.min_person_gap, 2), "lying_track_ids": len(lying_tids), "forced": loop.forced,
            "checkin_after_s": None if t_check is None or "appear" not in marks else round(t_check - marks["appear"], 1),
            "log": log[-60:] if reason != "ok" or os.environ.get("ANNIE_STORY_LOG") else []}


# ---------------------------------------------------------------------------------------------------------------
# fan out over processes (each run is mostly sleeping, but a process per run keeps the clocks honest)
# ---------------------------------------------------------------------------------------------------------------
def run_many(fn, seeds, *, workers=None, **kwargs) -> list[dict]:
    seeds = list(seeds)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 2, len(seeds)))
    if workers > 1:
        try:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                futures = [pool.submit(fn, seed, **kwargs) for seed in seeds]
                return [f.result(timeout=300) for f in futures]
        except (OSError, PermissionError, ImportError, RuntimeError):
            pass  # no subprocesses here (sandbox): fall through to one at a time
    return [fn(seed, **kwargs) for seed in seeds]
