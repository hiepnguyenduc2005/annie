"""4D (space + time) record of what the patrolling Go2 observed. Pure Python + numpy, no hardware.

`SpacetimeRecorder` keeps a bounded, thread-safe history of four streams so the browser page
`spacetime_viewer.html` can show the scene in 3D and scrub back through time:
  1. poses      - robot odometry (x, y, yaw) as reported by the robot: dead-reckoned, it drifts.
  2. obstacles  - LiDAR occupancy voxel centres (`go2_smart_patrol.voxel_points_world`) in the
                  odometry frame. These are occupancy ESTIMATES from the robot's own voxel map,
                  randomly subsampled per frame; not a surveyed map and not calibrated ranges.
  3. people     - person positions ESTIMATED by the runtime from camera bearing + LiDAR range;
                  `identity` and `posture` are model guesses, never a diagnosis.
  4. events     - greet / checkin / collision / voice / brain markers at the pose they happened.
Every timestamp is the HOST RECEIPT time supplied by the caller (seconds, one clock for all
streams), not a sensor capture time, so streams can be skewed by transport latency.

Memory is bounded three ways: records older than `max_seconds` are dropped, each stream has a
hard cap (`max_frames` for obstacle frames), and high-rate input is thinned on the way in
(`min_frame_dt`, `pose_min_dt`, `people_min_dt`). `snapshot()` additionally returns at most
`max_frames_out` evenly spaced frames so a full 15-minute run stays a few MB of JSON.

With `path=...` each ACCEPTED record is appended as one JSON line so a run can be replayed with
`load_jsonl`. That file holds person identities and event text: keep it in an ignored directory.
`spacetime_http` maps the three GET routes onto a recorder for the stdlib live-view server.
"""
from __future__ import annotations

import json
import math
import operator
import threading
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VIEWER_PATH = Path(__file__).resolve().parents[1] / "view" / "spacetime_viewer.html"
THREE_LOCAL_PATH = Path(__file__).resolve().parents[1] / "view" / "three.min.js"  # optional vendored r128 for offline (robot hotspot) use
EVENT_KINDS = ("greet", "checkin", "collision", "voice", "brain")  # the page has icons for these; others still record
MAX_FRAMES_OUT = 300


def _num(v, nd=2):
    """Finite float rounded to `nd` decimals, else None (NaN/inf are not valid JSON)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def _text(v, limit):
    return None if v is None else str(v)[:limit]


def _track_id(v):
    if v is None or isinstance(v, str):
        return _text(v, 40)
    try:
        return operator.index(v)  # python and numpy integers
    except TypeError:
        return str(v)[:40]


def _even_indices(n: int, k: int) -> list:
    """At most `k` indices spread evenly over range(n); keeps the first and the last."""
    if k <= 0 or n <= 0:
        return []
    if n <= k:
        return list(range(n))
    if k == 1:
        return [n - 1]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _points_json(arr) -> list:
    """(N,3) float32 -> nested lists rounded to 2 decimals (via float64 so 1.23 prints as 1.23)."""
    import numpy as np
    return np.round(np.asarray(arr, dtype=np.float64), 2).tolist()


class SpacetimeRecorder:
    """Bounded, thread-safe history of poses, obstacle frames, people and events. See module docstring.

    `record_*` return True when the record was stored and False when it was invalid or thinned
    by a rate limit. `min_frame_dt=None` means `max_seconds / max_frames` (0.5 s by default), i.e.
    just slow enough that `max_frames` covers the whole `max_seconds` window.
    """

    def __init__(self, *, max_seconds=900.0, max_points_per_frame=400, max_frames=1800, path=None,
                 min_frame_dt=None, pose_min_dt=0.2, people_min_dt=0.25, max_poses=6000, max_people=8000,
                 max_events=1000, seed=0):
        import numpy as np
        self.max_seconds = float(max_seconds)
        self.max_points_per_frame = max(1, int(max_points_per_frame))
        self.max_frames = max(1, int(max_frames))
        self.min_frame_dt = self.max_seconds / self.max_frames if min_frame_dt is None else float(min_frame_dt)
        if not math.isfinite(self.min_frame_dt):
            self.min_frame_dt = 0.0
        self.pose_min_dt, self.people_min_dt = float(pose_min_dt), float(people_min_dt)
        self._poses = deque(maxlen=max(1, int(max_poses)))      # [t, x, y, yaw]
        self._frames = deque(maxlen=self.max_frames)            # (t, float32 (k,3))
        self._people = deque(maxlen=max(1, int(max_people)))    # dict per observation
        self._events = deque(maxlen=max(1, int(max_events)))    # dict per event
        self._pose_now = None        # freshest pose, even when thinned out of the history
        self._people_now = (None, [])
        self._track_last = {}        # track_id -> (t, identity, posture) of the last STORED observation
        self._t_first = self._t_last = None
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()
        self.path = Path(path) if path else None
        self.path_error = None
        self._fh = None

    # ---- recording -------------------------------------------------------------------------
    def record_pose(self, t, x, y, yaw) -> bool:
        """Robot pose in the odometry frame (metres / radians). Thinned to one per `pose_min_dt`."""
        rec = [_num(t, 3), _num(x, 3), _num(y, 3), _num(yaw, 3)]
        if None in rec:
            return False
        with self._lock:
            self._pose_now = rec
            if self._poses and 0.0 <= rec[0] - self._poses[-1][0] < self.pose_min_dt:
                return False
            self._poses.append(rec)
            self._commit(rec[0], {"type": "pose", "t": rec[0], "x": rec[1], "y": rec[2], "yaw": rec[3]})
        return True

    def record_obstacles(self, t, points_world) -> bool:
        """Occupied voxel centres (N,3) in the odometry frame; randomly subsampled to `max_points_per_frame`."""
        import numpy as np
        t = _num(t, 3)
        if t is None:
            return False
        with self._lock:
            if self._frames and 0.0 <= t - self._frames[-1][0] < self.min_frame_dt:
                return False
        try:
            pts = np.asarray(points_world, dtype=np.float32)
        except (TypeError, ValueError):
            return False
        if pts.ndim != 2 or pts.shape[1] < 3 or pts.shape[0] == 0:
            return False
        pts = pts[:, :3]
        pts = pts[np.isfinite(pts).all(axis=1)]
        if pts.shape[0] == 0:
            return False
        with self._lock:
            if pts.shape[0] > self.max_points_per_frame:
                pts = pts[self._rng.choice(pts.shape[0], self.max_points_per_frame, replace=False)]
            pts = np.round(pts, 2).astype(np.float32)
            self._frames.append((t, pts))
            self._commit(t, (lambda: {"type": "obstacles", "t": t, "points": _points_json(pts)}))
        return True

    def record_people(self, t, people) -> bool:
        """Person estimates: dicts with track_id, x, y, z, label, identity (name or None), posture.

        Each track is thinned to one stored observation per `people_min_dt`, except that a change of
        identity or posture (e.g. to "lying") is always stored. `latest()` sees every call in full.
        """
        t = _num(t, 3)
        if t is None:
            return False
        recs = []
        for p in people or ():
            if not isinstance(p, dict):
                continue
            x, y, z = _num(p.get("x")), _num(p.get("y")), _num(p.get("z"))
            if x is None or y is None:
                continue
            recs.append({"t": t, "track_id": _track_id(p.get("track_id")), "x": x, "y": y, "z": 0.0 if z is None else z,
                         "label": _text(p.get("label"), 80) or "person", "identity": _text(p.get("identity"), 80),
                         "posture": _text(p.get("posture"), 40)})
        with self._lock:
            self._people_now = (t, recs)
            kept = []
            for r in recs:
                last = self._track_last.get(r["track_id"])
                if (last and 0.0 <= t - last[0] < self.people_min_dt
                        and last[1:] == (r["identity"], r["posture"])):
                    continue
                self._track_last[r["track_id"]] = (t, r["identity"], r["posture"])
                kept.append(r)
            if not kept:
                return False
            self._people.extend(kept)
            self._commit(t, {"type": "people", "t": t, "people": [{k: v for k, v in r.items() if k != "t"} for r in kept]})
        return True

    def record_event(self, t, x, y, kind, text) -> bool:
        """greet / checkin / collision / voice / brain marker. x/y None falls back to the freshest pose."""
        t = _num(t, 3)
        if t is None:
            return False
        with self._lock:
            pose = self._pose_now
            x, y = _num(x), _num(y)
            rec = {"t": t, "x": x if x is not None else (round(pose[1], 2) if pose else 0.0),
                   "y": y if y is not None else (round(pose[2], 2) if pose else 0.0),
                   "kind": _text(kind, 40) or "event", "text": _text(text, 240) or ""}
            if self._events and self._events[-1] == rec:  # a retried event is the same event
                return False
            self._events.append(rec)
            self._commit(t, {"type": "event", **rec})
        return True

    def _commit(self, t, line):
        """Under the lock: advance the clock, drop what fell out of `max_seconds`, append the JSONL line."""
        self._t_last = t if self._t_last is None else max(self._t_last, t)
        cutoff = self._t_last - self.max_seconds
        for store, key in ((self._poses, lambda r: r[0]), (self._frames, lambda r: r[0]),
                           (self._people, lambda r: r["t"]), (self._events, lambda r: r["t"])):
            while store and key(store[0]) < cutoff:
                store.popleft()
        if len(self._track_last) > 256:
            self._track_last = {k: v for k, v in self._track_last.items() if v[0] >= cutoff}
        firsts = [s[0][0] for s in (self._poses, self._frames) if s] + [s[0]["t"] for s in (self._people, self._events) if s]
        self._t_first = min(firsts) if firsts else None
        if self.path is None or self.path_error:
            return
        try:
            if self._fh is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.path, "a", buffering=1, encoding="utf-8")
            self._fh.write(json.dumps(line() if callable(line) else line, separators=(",", ":"), allow_nan=False) + "\n")
        except (OSError, ValueError) as exc:  # a full disk must not stop the patrol; keep recording in memory
            self.path_error = str(exc)

    def close(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None

    # ---- reading ---------------------------------------------------------------------------
    def snapshot(self, since=None, until=None, *, max_frames_out=MAX_FRAMES_OUT, max_poses_out=3000,
                 max_people_out=4000) -> dict:
        """JSON-serialisable history with `since < t <= until` (either may be None).

        `since` is exclusive so a page can poll with `since=<previous t1>` and append without
        duplicates. Frames, poses and people are thinned to evenly spaced samples beyond their
        `max_*_out`; `counts` reports stored vs returned. `span` is the whole recorder's [first, last]
        time so a client can drop what the recorder has already forgotten.
        """
        with self._lock:
            poses, frames = list(self._poses), list(self._frames)
            people, events = list(self._people), list(self._events)
            span = [self._t_first, self._t_last]
        since, until = (None if v is None else float(v) for v in (since, until))

        def keep(t):
            return (since is None or t > since) and (until is None or t <= until)

        poses = [p for p in poses if keep(p[0])]
        frames = [f for f in frames if keep(f[0])]
        people = [p for p in people if keep(p["t"])]
        events = [e for e in events if keep(e["t"])]
        counts = {"poses_total": len(poses), "frames_total": len(frames), "people_total": len(people)}
        poses = [poses[i] for i in _even_indices(len(poses), int(max_poses_out))]
        frames = [frames[i] for i in _even_indices(len(frames), int(max_frames_out))]
        people = [people[i] for i in _even_indices(len(people), int(max_people_out))]
        times = [p[0] for p in poses] + [f[0] for f in frames] + [p["t"] for p in people] + [e["t"] for e in events]
        return {"t0": min(times) if times else None, "t1": max(times) if times else None, "span": span,
                "frame_id": "odom", "units": "metres, radians, seconds (host receipt time)",
                "counts": {**counts, "poses": len(poses), "frames": len(frames), "people": len(people),
                           "points": int(sum(len(f[1]) for f in frames))},
                "poses": [list(p) for p in poses],
                "frames": [{"t": t, "points": _points_json(pts)} for t, pts in frames],
                "people": [dict(p) for p in people], "events": [dict(e) for e in events]}

    def latest(self) -> dict:
        """Small live view: freshest pose, the people of the last `record_people` call, the last obstacle frame."""
        with self._lock:
            pose, (people_t, people) = self._pose_now, self._people_now
            frame = self._frames[-1] if self._frames else None
            events = list(self._events)[-5:]
            t = self._t_last
        return {"t": t, "pose": list(pose) if pose else None, "people_t": people_t, "people": [dict(p) for p in people],
                "frame": {"t": frame[0], "points": _points_json(frame[1])} if frame else None,
                "events": [dict(e) for e in events]}


def load_jsonl(path, **kwargs) -> SpacetimeRecorder:
    """Rebuild a recorder from a JSONL run log. Input thinning is off by default (the log is already
    thinned); malformed lines are skipped and counted in `.skipped_lines`. The result does not append to `path`."""
    opts = {"min_frame_dt": 0.0, "pose_min_dt": 0.0, "people_min_dt": 0.0, **kwargs, "path": None}
    rec = SpacetimeRecorder(**opts)
    rec.skipped_lines = 0
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
                kind = d["type"]
                if kind == "pose":
                    ok = rec.record_pose(d["t"], d["x"], d["y"], d["yaw"])
                elif kind == "obstacles":
                    ok = rec.record_obstacles(d["t"], d["points"])
                elif kind == "people":
                    ok = rec.record_people(d["t"], d["people"])
                elif kind == "event":
                    ok = rec.record_event(d["t"], d.get("x"), d.get("y"), d.get("kind"), d.get("text"))
                else:
                    ok = False
            except (ValueError, KeyError, TypeError):
                ok = False
            rec.skipped_lines += 0 if ok else 1
    return rec


def _query_float(query, name):
    raw = query.get(name, [None])[-1]
    if raw in (None, ""):
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(name)
    return value


def spacetime_http(recorder, raw_path):
    """GET router for the live-view server: (status, body bytes, content type), or None if the path is not ours.

      /spacetime  (or /spacetime.html)          the viewer page
      /spacetime.json[?since=<t>&until=<t>&max_frames=<n>]   `snapshot()`; since is exclusive, until inclusive
      /spacetime_latest.json                    `latest()`
      /three.min.js                             optional vendored Three.js r128 next to this file (offline fallback)
    The JSON carries person identities: serve it only where the live view itself may be served.
    """
    url = urlsplit(raw_path)
    route = url.path.rstrip("/") or "/"
    if route in ("/spacetime", "/spacetime.html"):
        try:
            return 200, VIEWER_PATH.read_bytes(), "text/html; charset=utf-8"
        except OSError:
            return 404, b"spacetime_viewer.html is missing", "text/plain"
    if route == "/three.min.js":
        try:
            return 200, THREE_LOCAL_PATH.read_bytes(), "application/javascript"
        except OSError:
            return 404, b"no vendored three.min.js", "text/plain"
    if route not in ("/spacetime.json", "/spacetime_latest.json"):
        return None
    if recorder is None:
        return 503, b'{"error":"spacetime recorder is not running"}', "application/json"
    if route == "/spacetime_latest.json":
        body = recorder.latest()
    else:
        query = parse_qs(url.query)
        try:
            since, until = _query_float(query, "since"), _query_float(query, "until")
            n = _query_float(query, "max_frames")
        except ValueError:
            return 400, b'{"error":"since, until and max_frames must be finite numbers"}', "application/json"
        n = MAX_FRAMES_OUT if n is None else max(1, min(int(n), 4 * MAX_FRAMES_OUT))
        body = recorder.snapshot(since=since, until=until, max_frames_out=n)
    return 200, json.dumps(body, separators=(",", ":"), allow_nan=False).encode(), "application/json"
