"""4D (x, y, z, t) knowledge graph of what the dog observed: a geometry layer and a semantics layer.

`SpacetimeGraph` turns the recorder's four streams (`memory/spacetime.py`: poses, LiDAR obstacle
frames, people/object sightings, events) into something an agent can ASK: where was Jeanine at
09:45, what is within 1 m of this 3D point, how far is the nearest obstacle, which part of the
space has the dog not visited lately. Pure Python + numpy, no hardware, bounded memory, thread-safe.

Read this before trusting an answer:
  * POSITIONS ARE ODOMETRY-FRAME ESTIMATES. The robot's odometry is dead-reckoned and drifts; person
    positions come from camera bearing + a range guess. Nothing here is surveyed or calibrated.
  * PLACES ARE FREE-SPACE CLUSTERS, NOT ROOMS. "place-2" is a connected patch of floor the robot
    itself stood on, split at narrow passages. It has no walls, no door and no meaning until someone
    (or a language model, via `label_place`) names it - and that name is then a guess too.
  * LABELS ARE MODEL GUESSES. `label`, `identity` and `posture` come from detectors and a face
    matcher. "Jeanine, lying" is evidence of an observation, never an identification or a diagnosis.
  * TIMESTAMPS ARE HOST RECEIPT TIMES (seconds, the caller's clock), not sensor capture times, so
    streams can be skewed against each other by transport latency.
  * An occupied voxel is "LiDAR returned something here at least `min_hits` times". It does not decay
    with time, so a person who stood still leaves voxels behind (use `max_age_s`). The one exception:
    when the robot's own body later passes through a voxel (`body_clear_m`, below `body_top_m`), that
    voxel counts as free from then on until LiDAR sees it again; queries with an earlier `at` still
    see it. `nearest_obstacle` and `free_ahead` describe remembered geometry for reasoning; they are
    NOT a safety function and must never replace the patrol's live collision guard.

Layers
  geometry   fixed-size voxel grid (`cell_m`, default 0.2 m over 16 x 16 x 3 m centred on the first
             pose): per voxel first_seen, last_seen, hit count. Points outside the grid are counted
             and dropped. A coarse 2D grid (`place_cell_m`, 0.5 m) records where the robot's
             footprint has been (FREE space) with first/last visit times.
  places     derived from the free grid: regions that are at least 3 x 3 cells wide seed a place,
             narrow passages are attached to the nearest seed, anything left over is its own place.
             Numbers are stable ("place-1" stays "place-1" while it grows; a merge keeps the larger).
  semantics  entities keyed by identity when present, else `label#track_id`; sightings thinned to one
             per `sighting_min_dt` unless posture or place changes; "intervals" (observed stays at a
             place) are derived at query time against the CURRENT places, so they never go stale.
  events     greet / checkin / collision / voice / brain markers with the place they happened in.
"""
from __future__ import annotations

import bisect
import functools
import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np

# Exactly the properties of the strict `annie-spacetime` mapping (memory/elastic.py MAPPING).
ELASTIC_KEYS = ("@timestamp", "kind", "run_id", "x", "y", "z", "label", "identity", "posture", "track_id", "text")
_NBRS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
# sighting tuple layout
_T, _X, _Y, _Z, _POSTURE, _PLACE, _KEEP = range(7)


def _fnum(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean(v, limit):
    if v is None:
        return None
    s = str(v).strip()[:limit]
    return s or None


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def _ago(seconds) -> str:
    s = max(0.0, float(seconds))
    if s < 10:
        return "just now"
    if s < 90:
        return f"{int(round(s))} s ago"
    if s < 5400:
        return f"{int(round(s / 60))} min ago"
    return f"{s / 3600:.1f} h ago"


def _span(seconds) -> str:
    s = max(0.0, float(seconds))
    if s < 90:
        return f"{int(round(s))} s"
    if s < 5400:
        return f"{int(round(s / 60))} min"
    return f"{s / 3600:.1f} h"


def _clock(t) -> str:
    """Host-local HH:MM of a receipt time (small synthetic times fall back to seconds)."""
    try:
        if t < 86400:
            return f"t={t:.0f}s"
        return time.strftime("%H:%M", time.localtime(t))
    except (OverflowError, OSError, ValueError):
        return f"t={t:.0f}s"


def _iso(t) -> str:
    # same millisecond truncation as memory/elastic.py so a replay produces the same document _id
    return datetime.fromtimestamp(int(float(t) * 1000) / 1000.0, tz=timezone.utc).isoformat()


def _components(mask) -> list:
    """8-connected components of a 2D bool array, as lists of (i, j), in row-major discovery order."""
    seen = np.zeros(mask.shape, dtype=bool)
    ni, nj = mask.shape
    out = []
    for i, j in map(tuple, np.argwhere(mask)):
        if seen[i, j]:
            continue
        seen[i, j] = True
        comp, stack = [], [(i, j)]
        while stack:
            a, b = stack.pop()
            comp.append((a, b))
            for da, db in _NBRS:
                c, d = a + da, b + db
                if 0 <= c < ni and 0 <= d < nj and mask[c, d] and not seen[c, d]:
                    seen[c, d] = True
                    stack.append((c, d))
        out.append(sorted(comp))
    return out


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class _Entity:
    __slots__ = ("key", "kind", "label", "identity", "track_id", "sightings", "last", "first_seen", "n_seen")

    def __init__(self, key, kind, label, identity, track_id, t):
        self.key, self.kind, self.label, self.identity, self.track_id = key, kind, label, identity, track_id
        self.sightings = []   # thinned, sorted by t: (t, x, y, z, posture, place number at ingest, keep)
        self.last = None      # freshest sighting, even when thinning kept it out of `sightings`
        self.first_seen = t
        self.n_seen = 0

    @property
    def name(self):
        return self.identity or self.label


class SpacetimeGraph:
    """Bounded 4D memory with a geometry layer, places, entities and events. See the module docstring
    for what the numbers mean (odometry-frame estimates, free-space clusters, model guesses, host
    receipt times). Every query returns plain JSON-serialisable dicts; `at=None` means "as of the
    latest ingested record" (`t_last`), never the wall clock, so replays answer sensibly.
    """

    def __init__(self, *, cell_m=0.2, size_m=(16.0, 16.0, 3.0), center=None, z_min=-0.5, place_cell_m=0.5,
                 footprint_m=0.35, place_snap_m=2.0, core_min_cells=4, sighting_min_dt=2.0, stay_gap_s=120.0,
                 max_entities=500, max_sightings=400, max_events=1000, identity_hold_s=10.0,
                 object_merge_m=1.0, min_hits=1, body_clear_m=0.2, body_top_m=0.7, run_id="live"):
        self.cell_m, self.place_cell_m = float(cell_m), float(place_cell_m)
        self.size_m = tuple(float(v) for v in size_m)
        self.z_min, self.footprint_m, self.place_snap_m = float(z_min), float(footprint_m), float(place_snap_m)
        self.core_min_cells, self.min_hits = max(1, int(core_min_cells)), max(1, int(min_hits))
        self.sighting_min_dt, self.stay_gap_s = float(sighting_min_dt), float(stay_gap_s)
        self.max_entities, self.max_sightings = max(1, int(max_entities)), max(8, int(max_sightings))
        self.identity_hold_s, self.object_merge_m = float(identity_hold_s), float(object_merge_m)
        self.body_clear_m, self.body_top_m = float(body_clear_m), float(body_top_m)
        self.run_id = str(run_id)
        self.shape = tuple(max(1, int(math.ceil(s / self.cell_m - 1e-9))) for s in self.size_m)
        self.place_shape = tuple(max(1, int(math.ceil(s / self.place_cell_m - 1e-9))) for s in self.size_m[:2])
        self._center = None if center is None else (float(center[0]), float(center[1]))
        self._origin = None                      # (x0, y0, z0) of voxel [0, 0, 0]'s corner; set by the first data
        self._hits = self._first = self._last = self._cleared = None
        self._free_n = self._free_first = self._free_last = None
        self._place_grid = self._snap_grid = None  # place number per coarse cell (0 = none); snapped = dilated
        self._place_names, self._place_next = {}, 1
        self._places_dirty = False
        self._entities = {}
        self._track_last = {}                    # track_id -> (entity key, t, named)
        self._anon = 0
        self._events = deque(maxlen=max(1, int(max_events)))
        self._watermark = {"pose": None, "obstacles": None, "people": None, "event": None}
        self.pose_now = None
        self.t_first = self.t_last = None
        self.stats = {"poses": 0, "poses_out_of_grid": 0, "frames": 0, "points": 0, "points_out_of_grid": 0,
                      "sightings": 0, "sightings_stored": 0, "events": 0, "entities_dropped": 0, "rejected": 0}
        self.skipped_lines = 0
        self._lock = threading.RLock()

    # ---- grid plumbing -----------------------------------------------------------------------
    def _ensure_grid(self, cx, cy):
        if self._origin is not None:
            return
        if self._center is not None:
            cx, cy = self._center
        pc = self.place_cell_m  # snap so place cells and voxels share the origin
        self._origin = (math.floor((cx - self.size_m[0] / 2) / pc) * pc, math.floor((cy - self.size_m[1] / 2) / pc) * pc,
                        self.z_min)
        n = int(np.prod(self.shape))
        self._hits = np.zeros(n, dtype=np.uint32)
        self._first = np.full(n, np.nan)
        self._last = np.full(n, np.nan)
        self._cleared = np.full(n, np.nan)  # when the robot's body last passed through the voxel
        self._free_n = np.zeros(self.place_shape, dtype=np.uint32)
        self._free_first = np.full(self.place_shape, np.nan)
        self._free_last = np.full(self.place_shape, np.nan)
        self._place_grid = np.zeros(self.place_shape, dtype=np.int32)
        self._snap_grid = np.zeros(self.place_shape, dtype=np.int32)

    def _tick(self, t):
        self.t_first = t if self.t_first is None else min(self.t_first, t)
        self.t_last = t if self.t_last is None else max(self.t_last, t)

    def _now(self, at):
        at = _fnum(at)
        return at if at is not None else (self.t_last if self.t_last is not None else 0.0)

    def _voxel_centres(self, flat):
        i, j, k = np.unravel_index(flat, self.shape)
        o, c = self._origin, self.cell_m
        return np.stack([o[0] + (i + 0.5) * c, o[1] + (j + 0.5) * c, o[2] + (k + 0.5) * c], axis=1)

    def _occupied(self, at=None, min_hits=None, max_age_s=None):
        """Flat indices of voxels occupied as of `at`: enough hits, first seen at/before `at`, and (optionally)
        seen again within `max_age_s`. Hit counts are totals, so `at` filters by first_seen only."""
        if self._hits is None:
            return np.zeros(0, dtype=np.int64)
        mask = self._hits >= (self.min_hits if min_hits is None else max(1, int(min_hits)))
        if at is not None:
            mask &= self._first <= at
        # the robot stood here after the last LiDAR hit: free since then (NaN compares False = never cleared)
        mask &= ~((self._cleared <= (np.inf if at is None else at)) & (self._last < self._cleared))
        if max_age_s is not None:
            mask &= self._last >= self._now(at) - float(max_age_s)
        return np.flatnonzero(mask)

    def _voxel_dict(self, flat, centre):
        return {"x": _r(centre[0]), "y": _r(centre[1]), "z": _r(centre[2]), "hits": int(self._hits[flat]),
                "first_seen": _r(self._first[flat], 3), "last_seen": _r(self._last[flat], 3)}

    # ---- geometry layer ----------------------------------------------------------------------
    @_locked
    def ingest_obstacles(self, t, points_world) -> bool:
        """LiDAR occupied voxel centres (N,3), odometry frame, metres. Each voxel is counted once per frame."""
        t = _fnum(t)
        if t is None:
            self.stats["rejected"] += 1
            return False
        try:
            pts = np.asarray(points_world, dtype=np.float64)
        except (TypeError, ValueError):
            self.stats["rejected"] += 1
            return False
        if pts.ndim != 2 or pts.shape[1] < 3 or pts.shape[0] == 0:
            self.stats["rejected"] += 1
            return False
        pts = pts[:, :3]
        pts = pts[np.isfinite(pts).all(axis=1)]
        if pts.shape[0] == 0:
            self.stats["rejected"] += 1
            return False
        self._ensure_grid(float(np.median(pts[:, 0])), float(np.median(pts[:, 1])))
        idx = np.floor((pts - np.asarray(self._origin)) / self.cell_m + 1e-9).astype(np.int64)
        ok = ((idx >= 0) & (idx < np.asarray(self.shape))).all(axis=1)
        self.stats["points_out_of_grid"] += int((~ok).sum())
        self.stats["points"] += int(ok.sum())
        self.stats["frames"] += 1
        self._tick(t)
        if ok.any():
            flat = np.unique(np.ravel_multi_index(idx[ok].T, self.shape))
            self._hits[flat] += 1
            self._first[flat] = np.fmin(self._first[flat], t)
            self._last[flat] = np.fmax(self._last[flat], t)
        return True

    @_locked
    def ingest_pose(self, t, x, y, yaw=0.0) -> bool:
        """Robot pose (odometry frame). The footprint marks coarse cells as FREE with first/last visit times."""
        t, x, y, yaw = _fnum(t), _fnum(x), _fnum(y), _fnum(yaw)
        if t is None or x is None or y is None:
            self.stats["rejected"] += 1
            return False
        self._ensure_grid(x, y)
        self._tick(t)
        self.stats["poses"] += 1
        if self.pose_now is None or t >= self.pose_now[0]:
            self.pose_now = (t, x, y, 0.0 if yaw is None else yaw)
        pc, (ox, oy, _), (ni, nj) = self.place_cell_m, self._origin, self.place_shape
        ci, cj = int(math.floor((x - ox) / pc)), int(math.floor((y - oy) / pc))
        if not (0 <= ci < ni and 0 <= cj < nj):
            self.stats["poses_out_of_grid"] += 1
            return True
        # the cell under the robot, plus neighbours whose CENTRE is inside the footprint radius; a straight
        # walk is therefore at most 2 cells wide and can never look like a room (a 3 x 3 block)
        for i in range(max(0, ci - 1), min(ni, ci + 2)):
            for j in range(max(0, cj - 1), min(nj, cj + 2)):
                if (i, j) != (ci, cj) and math.hypot(ox + (i + 0.5) * pc - x, oy + (j + 0.5) * pc - y) > self.footprint_m:
                    continue
                if self._free_n[i, j] == 0:
                    self._places_dirty = True
                self._free_n[i, j] += 1
                self._free_first[i, j] = t if math.isnan(self._free_first[i, j]) else min(self._free_first[i, j], t)
                self._free_last[i, j] = t if math.isnan(self._free_last[i, j]) else max(self._free_last[i, j], t)
        if self._places_dirty:
            self._recompute_places()  # rare: only when a cell becomes free for the first time
        self._clear_body(t, x, y)
        return True

    def _clear_body(self, t, x, y):
        """Voxels whose centre is inside the robot's body right now cannot be occupied: stamp them cleared at t."""
        r, c, o = self.body_clear_m, self.cell_m, self._origin
        if r <= 0:
            return
        gi = np.arange(max(0, int(math.floor((x - r - o[0]) / c))), min(self.shape[0], int(math.floor((x + r - o[0]) / c)) + 1))
        gj = np.arange(max(0, int(math.floor((y - r - o[1]) / c))), min(self.shape[1], int(math.floor((y + r - o[1]) / c)) + 1))
        gk = np.arange(0, max(0, min(self.shape[2], int(math.floor((self.body_top_m - o[2]) / c)))))
        if not (gi.size and gj.size and gk.size):
            return
        ii, jj = np.meshgrid(gi, gj, indexing="ij")
        inside = np.hypot(o[0] + (ii + 0.5) * c - x, o[1] + (jj + 0.5) * c - y) <= r
        if not inside.any():
            return
        ii, jj = ii[inside], jj[inside]
        flat = np.ravel_multi_index((np.repeat(ii, gk.size), np.repeat(jj, gk.size), np.tile(gk, ii.size)), self.shape)
        self._cleared[flat] = np.fmax(self._cleared[flat], t)

    def _recompute_places(self):
        self._places_dirty = False
        free = self._free_n > 0
        ni, nj = free.shape
        pad = np.pad(free, 1, constant_values=False)
        core = free.copy()  # cells whose whole 3 x 3 neighbourhood is free: wide enough to be "a place"
        for di in range(3):
            for dj in range(3):
                core &= pad[di:di + ni, dj:dj + nj]
        region = np.zeros(free.shape, dtype=np.int32)
        k = 0
        for comp in _components(core):
            if len(comp) >= self.core_min_cells:
                k += 1
                for c in comp:
                    region[c] = k
        if k:  # grow the seeds through narrow free cells: nearest seed wins, ties by row-major order
            frontier = deque(map(tuple, np.argwhere(region > 0)))
            while frontier:
                a, b = frontier.popleft()
                for da, db in _NBRS:
                    c, d = a + da, b + db
                    if 0 <= c < ni and 0 <= d < nj and free[c, d] and region[c, d] == 0:
                        region[c, d] = region[a, b]
                        frontier.append((c, d))
        for comp in _components(free & (region == 0)):  # free space with no wide part is its own place
            k += 1
            for c in comp:
                region[c] = k
        # stable numbers: each region keeps the previous number it overlaps most (first come = oldest region)
        prev, grid, claimed = self._place_grid, np.zeros(free.shape, dtype=np.int32), set()
        regions = []
        for rid in range(1, k + 1):
            cells = np.argwhere(region == rid)
            regions.append((float(np.nanmin(self._free_first[cells[:, 0], cells[:, 1]])), tuple(cells[0]), cells))
        for _, _, cells in sorted(regions, key=lambda r: (r[0], r[1])):
            olds = prev[cells[:, 0], cells[:, 1]]
            counts = {}
            for o in olds[olds > 0].tolist():
                if o not in claimed:
                    counts[o] = counts.get(o, 0) + 1
            if counts:
                num = min(counts, key=lambda o: (-counts[o], o))
            else:
                num, self._place_next = self._place_next, self._place_next + 1
            claimed.add(num)
            grid[cells[:, 0], cells[:, 1]] = num
        for old in [o for o in self._place_names if o not in claimed]:  # a named place was absorbed: pass the name on
            name = self._place_names.pop(old)
            heirs = grid[prev == old]
            heirs = heirs[heirs > 0]
            if heirs.size:
                heir = int(np.bincount(heirs).argmax())
                self._place_names.setdefault(heir, name)
        self._place_grid = grid
        # snapped grid: every cell within `place_snap_m` of a place takes the nearest place's number
        snap, dist = grid.copy(), np.where(grid > 0, 0, -1)
        rings = int(math.ceil(self.place_snap_m / self.place_cell_m))
        frontier = deque(map(tuple, np.argwhere(grid > 0)))
        while frontier:
            a, b = frontier.popleft()
            if dist[a, b] >= rings:
                continue
            for da, db in _NBRS:
                c, d = a + da, b + db
                if 0 <= c < ni and 0 <= d < nj and dist[c, d] < 0:
                    dist[c, d], snap[c, d] = dist[a, b] + 1, snap[a, b]
                    frontier.append((c, d))
        self._snap_grid = snap

    def _place_lookup(self, x, y):
        """(place number or None, "in" | "near" | None) for one point, against the current places."""
        if self._origin is None:
            return None, None
        pc, (ox, oy, _) = self.place_cell_m, self._origin
        i, j = int(math.floor((x - ox) / pc)), int(math.floor((y - oy) / pc))
        if not (0 <= i < self.place_shape[0] and 0 <= j < self.place_shape[1]):
            return None, None
        num = int(self._snap_grid[i, j])
        if num == 0:
            return None, None
        return num, ("in" if self._place_grid[i, j] == num else "near")

    def _place_id(self, num):
        return None if num is None else f"place-{num}"

    def _place_phrase(self, num, relation):
        if num is None:
            return "outside the explored area"
        name = self._place_names.get(num)
        where = f"{name} (place-{num})" if name else f"place-{num}"
        return f"{'near' if relation == 'near' else 'in'} {where}"

    def _place_fields(self, x, y):
        num, rel = self._place_lookup(x, y)
        return {"place": self._place_id(num), "place_name": self._place_names.get(num), "place_relation": rel}

    @_locked
    def places(self, include_cells=False) -> list:
        """Free-space clusters the robot has stood in (NOT rooms): id, optional name, centroid, extent, visit times."""
        if self._place_grid is None:
            return []
        pc, (ox, oy, _) = self.place_cell_m, self._origin
        out = []
        for num in sorted(int(n) for n in np.unique(self._place_grid) if n > 0):
            cells = np.argwhere(self._place_grid == num)
            xs, ys = ox + (cells[:, 0] + 0.5) * pc, oy + (cells[:, 1] + 0.5) * pc
            sel = (cells[:, 0], cells[:, 1])
            place = {"id": f"place-{num}", "name": self._place_names.get(num), "centroid": [_r(xs.mean()), _r(ys.mean())],
                     "extent": {"min": [_r(xs.min() - pc / 2), _r(ys.min() - pc / 2)],
                                "max": [_r(xs.max() + pc / 2), _r(ys.max() + pc / 2)]},
                     "n_cells": int(len(cells)), "area_m2": _r(len(cells) * pc * pc),
                     "first_visit": _r(np.nanmin(self._free_first[sel]), 3), "last_visit": _r(np.nanmax(self._free_last[sel]), 3),
                     "pose_samples": int(self._free_n[sel].sum())}
            if include_cells:
                place["cell_m"] = pc
                place["cells"] = [[_r(a), _r(b)] for a, b in zip(xs.tolist(), ys.tolist())]
            out.append(place)
        return out

    @_locked
    def label_place(self, place_id, name) -> bool:
        """Attach a human/LLM name to a place ("place-2" -> "kitchen"). The name is a guess; None clears it."""
        try:
            num = int(str(place_id).rsplit("-", 1)[-1])
        except ValueError:
            return False
        if self._place_grid is None or not (self._place_grid == num).any():
            return False
        name = _clean(name, 60)
        if name is None:
            self._place_names.pop(num, None)
        else:
            self._place_names[num] = name
        return True

    @_locked
    def place_of(self, x, y) -> dict:
        """Which place a ground point falls in ("in") or is within `place_snap_m` of ("near")."""
        x, y = _fnum(x), _fnum(y)
        if x is None or y is None:
            return {"place": None, "place_name": None, "place_relation": None}
        return self._place_fields(x, y)

    # ---- semantics layer ---------------------------------------------------------------------
    @_locked
    def ingest_sighting(self, t, entity) -> bool:
        """One person/object estimate (the recorder's dict: track_id, x, y, z, label, identity, posture, optional
        kind = "person" | "object"). Returns True when the sighting was STORED (False = invalid or thinned; a
        thinned sighting still refreshes the entity's latest position)."""
        t = _fnum(t)
        if t is None or not isinstance(entity, dict):
            self.stats["rejected"] += 1
            return False
        x, y, z = _fnum(entity.get("x")), _fnum(entity.get("y")), _fnum(entity.get("z"))
        if x is None or y is None:
            self.stats["rejected"] += 1
            return False
        z = 0.0 if z is None else z
        identity = _clean(entity.get("identity"), 80)
        label = _clean(entity.get("label"), 80) or identity or "person"
        posture = _clean(entity.get("posture"), 40)
        tid = entity.get("track_id")
        if tid is not None and not isinstance(tid, (int, str)):
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                tid = str(tid)[:40]
        kind = entity.get("kind")
        if kind not in ("person", "object"):
            kind = "person" if identity or label.lower().startswith("person") else "object"
        self._tick(t)
        self.stats["sightings"] += 1

        prev = self._track_last.get(tid) if tid is not None else None
        fresh = prev is not None and 0.0 <= t - prev[1] <= self.identity_hold_s and prev[0] in self._entities
        if identity:
            key = identity
            if fresh and not prev[2] and prev[0] != key:
                self._merge_entities(prev[0], key, identity)  # the same tracker track just got a name
        elif fresh and prev[2]:
            key = prev[0]                                      # a named track keeps its name through a flicker
        elif kind == "object":
            key = self._nearby_object(label, x, y, z)
            if key is None:
                self._anon += 1
                key = f"{label}#{tid if tid is not None else 'n%d' % self._anon}"
        else:
            key = f"{label}#{tid if tid is not None else '?'}"
        if tid is not None:
            self._track_last[tid] = (key, t, bool(identity) or bool(fresh and prev[2]))
            if len(self._track_last) > 1024:
                cutoff = sorted(v[1] for v in self._track_last.values())[len(self._track_last) // 2]
                self._track_last = {k: v for k, v in self._track_last.items() if v[1] >= cutoff}

        ent = self._entities.get(key)
        if ent is None:
            ent = self._entities[key] = _Entity(key, kind, label, identity, tid if isinstance(tid, int) else None, t)
            self._cap_entities(keep=key)
        if identity:
            ent.identity, ent.label = identity, (label if ent.label.lower().startswith("person") else ent.label)
        if isinstance(tid, int):
            ent.track_id = tid
        ent.n_seen += 1
        ent.first_seen = min(ent.first_seen, t)
        place = self._place_lookup(x, y)[0]
        if ent.last is None or t >= ent.last[_T]:
            ent.last = (t, x, y, z, posture, place, False)
        stored = ent.sightings
        if not stored or t >= stored[-1][_T]:
            prior = stored[-1] if stored else None
            changed = prior is None or posture != prior[_POSTURE] or place != prior[_PLACE]
            if not changed and t - prior[_T] < self.sighting_min_dt:
                return False
            stored.append((t, x, y, z, posture, place, changed))
        else:  # late arrival: keep it only if it fills a gap
            pos = bisect.bisect_left(stored, t, key=lambda s: s[_T])
            near = [stored[p][_T] for p in (pos - 1, pos) if 0 <= p < len(stored)]
            if any(abs(t - n) < self.sighting_min_dt for n in near):
                return False
            stored.insert(pos, (t, x, y, z, posture, place, False))
        self.stats["sightings_stored"] += 1
        if len(stored) > self.max_sightings:
            self._thin_sightings(ent)
        return True

    @_locked
    def ingest_people(self, t, people) -> int:
        """Convenience for the recorder's `record_people(t, people)` shape. Returns how many were stored."""
        return sum(1 for p in (people or ()) if self.ingest_sighting(t, p))

    def _nearby_object(self, label, x, y, z):
        best, best_d = None, self.object_merge_m
        for ent in self._entities.values():
            if ent.kind == "object" and ent.label == label and ent.last is not None:
                d = math.dist((x, y, z), ent.last[_X:_Z + 1])
                if d <= best_d:
                    best, best_d = ent.key, d
        return best

    def _merge_entities(self, src_key, dst_key, identity):
        src = self._entities.pop(src_key, None)
        if src is None:
            return
        dst = self._entities.get(dst_key)
        if dst is None:
            src.key, src.identity, src.label = dst_key, identity, identity
            self._entities[dst_key] = src
            return
        dst.sightings = sorted(dst.sightings + src.sightings, key=lambda s: s[_T])
        dst.first_seen, dst.n_seen = min(dst.first_seen, src.first_seen), dst.n_seen + src.n_seen
        if src.last is not None and (dst.last is None or src.last[_T] > dst.last[_T]):
            dst.last = src.last
        if len(dst.sightings) > self.max_sightings:
            self._thin_sightings(dst)

    def _thin_sightings(self, ent):
        """Over the cap: halve the density of the OLDER half, keeping change points; then hard-drop the oldest."""
        s = ent.sightings
        half = len(s) // 2
        older = [rec for n, rec in enumerate(s[:half]) if rec[_KEEP] or n % 2 == 0]
        ent.sightings = older + s[half:]
        if len(ent.sightings) > self.max_sightings:
            ent.sightings = ent.sightings[-self.max_sightings:]

    def _cap_entities(self, keep=None):
        while len(self._entities) > self.max_entities:
            pool = [e for e in self._entities.values() if not e.identity and e.key != keep] or \
                   [e for e in self._entities.values() if e.key != keep]
            victim = min(pool, key=lambda e: (e.last[_T] if e.last else e.first_seen, e.key))
            del self._entities[victim.key]
            self.stats["entities_dropped"] += 1

    # ---- events ------------------------------------------------------------------------------
    @_locked
    def ingest_event(self, t, x, y, kind, text) -> bool:
        """greet / checkin / collision / voice / brain marker. x/y None falls back to the freshest pose."""
        t = _fnum(t)
        if t is None:
            self.stats["rejected"] += 1
            return False
        x, y = _fnum(x), _fnum(y)
        pose = self.pose_now
        rec = {"t": t, "x": x if x is not None else (pose[1] if pose else 0.0),
               "y": y if y is not None else (pose[2] if pose else 0.0),
               "kind": _clean(kind, 40) or "event", "text": _clean(text, 240) or ""}
        if self._events and self._events[-1] == rec:  # a retried event is the same event
            return False
        self._events.append(rec)
        self._tick(t)
        self.stats["events"] += 1
        return True

    def _event_dict(self, e):
        return {"t": _r(e["t"], 3), "x": _r(e["x"]), "y": _r(e["y"]), "kind": e["kind"], "text": e["text"],
                **self._place_fields(e["x"], e["y"])}

    # ---- entity helpers ----------------------------------------------------------------------
    def _sighting_at(self, ent, at):
        """Latest sighting with t <= at (the freshest one when at is None)."""
        if at is None or (ent.last is not None and ent.last[_T] <= at):
            return ent.last
        pos = bisect.bisect_right(ent.sightings, at, key=lambda s: s[_T])
        return ent.sightings[pos - 1] if pos else None

    def _find(self, name, at=None):
        want = str(name or "").strip().lower()
        if not want:
            return None
        hits = [e for e in self._entities.values()
                if want in ((e.identity or "").lower(), e.label.lower(), e.key.lower())]
        hits = [(self._sighting_at(e, at), e) for e in hits]
        hits = [(s, e) for s, e in hits if s is not None]
        return max(hits, key=lambda h: (h[0][_T], h[1].key))[1] if hits else None

    def _sighting_dict(self, ent, s, now):
        return {"name": ent.name, "key": ent.key, "kind": ent.kind, "label": ent.label, "identity": ent.identity,
                "t": _r(s[_T], 3), "x": _r(s[_X]), "y": _r(s[_Y]), "z": _r(s[_Z]), "posture": s[_POSTURE],
                "age_s": _r(max(0.0, now - s[_T]), 1), **self._place_fields(s[_X], s[_Y])}

    def _intervals(self, ent) -> list:
        """Observed stays: runs of sightings at one place with no gap longer than `stay_gap_s`, judged against
        the CURRENT places. The entity was not necessarily there between two sightings."""
        seq = list(ent.sightings)
        if ent.last is not None and (not seq or ent.last[_T] > seq[-1][_T]):
            seq.append(ent.last)
        runs = []
        for s in seq:
            num = self._place_lookup(s[_X], s[_Y])[0]
            cur = runs[-1] if runs else None
            if cur and cur["num"] == num and s[_T] - cur["end"] <= self.stay_gap_s:
                cur["end"], cur["n"] = s[_T], cur["n"] + 1
                if s[_POSTURE] and s[_POSTURE] not in cur["postures"]:
                    cur["postures"].append(s[_POSTURE])
            else:
                runs.append({"num": num, "start": s[_T], "end": s[_T], "n": 1, "postures": [s[_POSTURE]] if s[_POSTURE] else []})
        out = []
        for n, run in enumerate(runs):
            nxt = runs[n + 1] if n + 1 < len(runs) else None
            if (run["n"] == 1 and out and nxt and out[-1]["num"] == nxt["num"]
                    and nxt["start"] - out[-1]["end"] <= self.stay_gap_s):
                continue  # a single noisy estimate across a place boundary is not a visit
            if out and out[-1]["num"] == run["num"] and run["start"] - out[-1]["end"] <= self.stay_gap_s:
                out[-1]["end"], out[-1]["n"] = run["end"], out[-1]["n"] + run["n"]
                out[-1]["postures"] += [p for p in run["postures"] if p not in out[-1]["postures"]]
            else:
                out.append(dict(run, postures=list(run["postures"])))
        return [{"place": self._place_id(r["num"]), "place_name": self._place_names.get(r["num"]), "start": _r(r["start"], 3),
                 "end": _r(r["end"], 3), "duration_s": _r(r["end"] - r["start"], 1), "n_sightings": r["n"],
                 "postures": r["postures"]} for r in out]

    # ---- queries -----------------------------------------------------------------------------
    @_locked
    def where_is(self, name, at=None) -> dict:
        """Last sighting of a person/object at or before `at` (identity, label or key; case-insensitive)."""
        at = _fnum(at)
        ent = self._find(name, at)
        if ent is None:
            return {"found": False, "name": str(name), "at": at}
        now = self._now(at)
        s = self._sighting_at(ent, at)
        out = {"found": True, "at": at, **self._sighting_dict(ent, s, now)}
        out["text"] = (f"{ent.name} last seen {_ago(now - s[_T])} {self._place_phrase(*self._place_lookup(s[_X], s[_Y]))}"
                       f"{', ' + s[_POSTURE] if s[_POSTURE] and s[_POSTURE] != 'unknown' else ''}")
        return out

    @_locked
    def timeline(self, name) -> dict:
        """Observed stays of one entity, oldest first: place, start, end, duration, postures seen."""
        ent = self._find(name)
        if ent is None:
            return {"found": False, "name": str(name), "intervals": [], "text": []}
        intervals = self._intervals(ent)
        text = []
        for iv in intervals:
            num = None if iv["place"] is None else int(iv["place"].rsplit("-", 1)[-1])
            where = "outside the explored area" if num is None else self._place_phrase(num, "in")
            text.append(f"{ent.name} was {where} from {_clock(iv['start'])} to {_clock(iv['end'])} "
                        f"({_span(iv['duration_s'])}, {iv['n_sightings']} sightings)")
        return {"found": True, "name": ent.name, "key": ent.key, "kind": ent.kind, "first_seen": _r(ent.first_seen, 3),
                "last_seen": _r(ent.last[_T], 3), "intervals": intervals, "text": text}

    @_locked
    def nearest_obstacle(self, x, y, z, at=None, *, min_hits=None, max_age_s=None, z_tol_m=None) -> dict:
        """True 3D distance from a point to the nearest voxel CENTRE occupied at/before `at` (so +-cell_m/2).
        `z_tol_m` restricts to voxels within that height of `z`. Remembered geometry, not a safety check."""
        p = np.array([_fnum(x), _fnum(y), _fnum(z)], dtype=object)
        if any(v is None for v in p):
            return {"found": False, "error": "x, y, z must be finite"}
        p = p.astype(np.float64)
        at = _fnum(at)
        flat = self._occupied(at, min_hits, max_age_s)
        centres = self._voxel_centres(flat) if flat.size else np.zeros((0, 3))
        if z_tol_m is not None and flat.size:
            keep = np.abs(centres[:, 2] - p[2]) <= float(z_tol_m)
            flat, centres = flat[keep], centres[keep]
        if not flat.size:
            return {"found": False, "at": at, "distance_m": None, "voxel": None, "cell_m": self.cell_m}
        d = np.linalg.norm(centres - p, axis=1)
        n = int(d.argmin())
        return {"found": True, "at": at, "distance_m": _r(d[n], 3), "voxel": self._voxel_dict(int(flat[n]), centres[n]),
                "cell_m": self.cell_m}

    @_locked
    def what_is_near(self, x, y, z, radius_m, at=None, *, min_hits=None, max_age_s=None) -> dict:
        """Entities (by their last sighting at/before `at`), events and obstacle density within `radius_m` of a
        3D point, all by true 3D distance (events sit at z = 0)."""
        p = [_fnum(x), _fnum(y), _fnum(z)]
        radius = _fnum(radius_m)
        if any(v is None for v in p) or radius is None or radius <= 0:
            return {"error": "x, y, z must be finite and radius_m positive", "entities": [], "events": []}
        at = _fnum(at)
        now = self._now(at)
        ents = []
        for ent in self._entities.values():
            s = self._sighting_at(ent, at)
            if s is None:
                continue
            d = math.dist(p, s[_X:_Z + 1])
            if d <= radius:
                ents.append({"distance_m": _r(d, 3), **self._sighting_dict(ent, s, now)})
        ents.sort(key=lambda e: (e["distance_m"], e["key"]))
        events = []
        for e in self._events:
            if at is not None and e["t"] > at:
                continue
            d = math.dist(p, (e["x"], e["y"], 0.0))
            if d <= radius:
                events.append({"distance_m": _r(d, 3), **self._event_dict(e)})
        events = sorted(events, key=lambda e: -e["t"])[:20]
        flat = self._occupied(at, min_hits, max_age_s)
        obstacles = {"occupied_voxels": 0, "voxels_in_sphere": 0, "density": 0.0, "nearest_m": None, "cell_m": self.cell_m}
        if self._origin is not None:
            c, o = self.cell_m, self._origin
            grids = [np.arange(max(0, int(math.floor((p[a] - radius - o[a]) / c))),
                               min(self.shape[a], int(math.floor((p[a] + radius - o[a]) / c)) + 1)) for a in range(3)]
            if all(g.size for g in grids):
                gi, gj, gk = np.meshgrid(*grids, indexing="ij")
                cen = np.stack([o[0] + (gi + 0.5) * c, o[1] + (gj + 0.5) * c, o[2] + (gk + 0.5) * c], axis=-1)
                obstacles["voxels_in_sphere"] = int((np.linalg.norm(cen - np.asarray(p), axis=-1) <= radius).sum())
            if flat.size:
                d = np.linalg.norm(self._voxel_centres(flat) - np.asarray(p), axis=1)
                inside = int((d <= radius).sum())
                obstacles["occupied_voxels"] = inside
                obstacles["nearest_m"] = _r(d.min(), 3) if inside else None
                if obstacles["voxels_in_sphere"]:
                    obstacles["density"] = _r(inside / obstacles["voxels_in_sphere"], 4)
        return {"at": at, "center": [_r(v) for v in p], "radius_m": radius, "entities": ents, "events": events,
                "obstacles": obstacles, **self._place_fields(p[0], p[1])}

    @_locked
    def free_ahead(self, x, y, heading, max_m, at=None, *, half_width_m=0.2, z_band=(0.0, 0.8), min_hits=None,
                   max_age_s=None) -> dict:
        """How far a `2 * half_width_m` wide corridor from (x, y) along `heading` (radians) stays clear of
        remembered occupied voxels in the height band `z_band`. `visited_m` is how much of that stretch the
        robot has actually stood on. Remembered geometry only - unknown space counts as free."""
        x, y, heading, max_m = _fnum(x), _fnum(y), _fnum(heading), _fnum(max_m)
        if None in (x, y, heading, max_m) or max_m <= 0:
            return {"error": "x, y, heading must be finite and max_m positive"}
        at = _fnum(at)
        flat = self._occupied(at, min_hits, max_age_s)
        free_m, hit = max_m, None
        c, s = math.cos(heading), math.sin(heading)
        if flat.size:
            cen = self._voxel_centres(flat)
            dx, dy = cen[:, 0] - x, cen[:, 1] - y
            along, lateral = dx * c + dy * s, -dx * s + dy * c
            half = self.cell_m / 2
            sel = ((along > 0) & (along <= max_m + half) & (np.abs(lateral) <= half_width_m + half)
                   & (cen[:, 2] >= z_band[0]) & (cen[:, 2] <= z_band[1]))
            if sel.any():
                n = int(np.flatnonzero(sel)[along[sel].argmin()])
                free_m = float(min(max_m, max(0.0, along[n] - half)))
                hit = self._voxel_dict(int(flat[n]), cen[n])
        visited, step = 0.0, self.place_cell_m / 2
        if self._free_n is not None:
            for n in range(1, int(free_m / step) + 1):
                num, rel = self._place_lookup(x + c * n * step, y + s * n * step)
                if rel != "in":
                    break
                visited = n * step
        return {"at": at, "free_m": _r(free_m, 2), "blocked": hit is not None, "hit": hit, "max_m": max_m,
                "visited_m": _r(min(visited, free_m), 2), "heading": _r(heading, 3)}

    @_locked
    def summary(self, now=None, limit=8) -> dict:
        """Short English sentences for a language-model prompt: {"now", "sentences": [...], "text": "..."}."""
        now = self._now(now)
        limit = max(1, int(limit))
        seen = [(self._sighting_at(e, now), e) for e in self._entities.values()]
        seen = [(s, e) for s, e in seen if s is not None]
        rank = lambda se: (0 if se[1].identity else 1 if se[1].kind == "object" else 2, -se[0][_T], se[1].key)  # noqa: E731
        objects = [(s, e) for s, e in seen if e.kind == "object"]
        ent_lines = []
        strangers = [(s, e) for s, e in seen if e.kind == "person" and not e.identity]
        recent_ids = sum(1 for s, _ in strangers if now - s[_T] <= 120.0)
        for s, e in sorted(seen, key=rank):
            if e.kind == "person" and not e.identity and (s, e) != max(strangers, key=lambda se: (se[0][_T], se[1].key)):
                continue  # tracker ids churn: report the freshest unnamed person once, not one line per id
            near = ""
            landmark = min(((math.dist(s[_X:_Z + 1], o[_X:_Z + 1]), oe.label) for o, oe in objects if oe is not e),
                           default=None)
            if landmark and landmark[0] <= 1.5:
                near = f" near the {landmark[1]}"
            named = bool(e.identity) or e.kind == "object"
            posture = f", {s[_POSTURE]}" if s[_POSTURE] and s[_POSTURE] != "unknown" else ""
            tail = "" if named else f" ({recent_ids} tracker id(s) in the last 2 min; ids churn, not a head count)"
            ent_lines.append(f"{e.name if named else 'an unidentified person'} last seen {_ago(now - s[_T])}{near} "
                             f"{self._place_phrase(*self._place_lookup(s[_X], s[_Y]))}{posture}{tail}")
        place_lines = []
        here = self._place_lookup(self.pose_now[1], self.pose_now[2])[0] if self.pose_now else None
        all_places = [p for p in self.places() if p["first_visit"] <= now]
        for p in sorted(all_places, key=lambda p: p["last_visit"]):
            idle = now - p["last_visit"]
            if idle >= 120.0 and p["id"] != self._place_id(here):
                label = f"{p['name']} ({p['id']})" if p["name"] else p["id"]
                place_lines.append(f"{label} not visited since {_clock(p['last_visit'])} ({_ago(idle)})")
        overview = []
        if all_places or self._hits is not None:
            n_vox = int(self._occupied(now).size)
            span = _span(now - self.t_first) if self.t_first is not None else "0 s"
            where = f"; the dog is {self._place_phrase(here, 'in')}" if here else ""
            overview.append(f"{len(all_places)} place(s) explored over {span}, {n_vox} occupied voxels remembered{where}")
        room = max(0, limit - min(len(place_lines), 2) - len(overview))
        sentences = (ent_lines[:room] + place_lines[:2] + overview)[:limit]
        return {"now": _r(now, 3), "sentences": sentences, "text": ". ".join(sentences) + ("." if sentences else "")}

    @_locked
    def voxels(self, at=None, max_points=4000, *, min_hits=None, max_age_s=None) -> dict:
        """Geometry layer for the viewer: occupied voxel centres as [x, y, z, hits, first_seen, last_seen], the
        most-hit first when capped. first_seen lets a page scrub time without asking again."""
        flat = self._occupied(_fnum(at), min_hits, max_age_s)
        total = int(flat.size)
        if total > max(0, int(max_points)):
            order = np.argsort(-self._hits[flat].astype(np.int64), kind="stable")[:max(0, int(max_points))]
            flat = np.sort(flat[order])
        cen = self._voxel_centres(flat) if flat.size else np.zeros((0, 3))
        pts = [[_r(c[0]), _r(c[1]), _r(c[2]), int(self._hits[f]), _r(self._first[f], 3), _r(self._last[f], 3)]
               for f, c in zip(flat.tolist(), cen.tolist())]
        return {"n": total, "returned": len(pts), "cell_m": self.cell_m, "size_m": list(self.size_m),
                "origin": None if self._origin is None else [_r(v) for v in self._origin],
                "columns": ["x", "y", "z", "hits", "first_seen", "last_seen"], "points": pts}

    @_locked
    def snapshot(self, *, max_entities_out=100, max_events_out=200, max_track_out=60, max_voxels=0) -> dict:
        """Bounded JSON view for the 4D viewer: places (with cells), entities (latest + intervals + thinned track),
        events, voxel summary (`max_voxels` > 0 embeds `voxels()` points)."""
        now = self._now(None)
        ents = sorted((e for e in self._entities.values() if e.last is not None),
                      key=lambda e: (0 if e.identity else 1, -e.last[_T], e.key))[:max(0, int(max_entities_out))]
        entities = []
        for e in ents:
            track = e.sightings
            if len(track) > max_track_out > 0:
                idx = sorted({round(n * (len(track) - 1) / (max_track_out - 1)) for n in range(max_track_out)}) \
                    if max_track_out > 1 else [len(track) - 1]
                track = [track[n] for n in idx]
            entities.append({**self._sighting_dict(e, e.last, now), "first_seen": _r(e.first_seen, 3),
                             "last_seen": _r(e.last[_T], 3), "n_seen": e.n_seen, "intervals": self._intervals(e)[-20:],
                             "track": [[_r(s[_T], 3), _r(s[_X]), _r(s[_Y]), _r(s[_Z]), s[_POSTURE]] for s in track]})
        vox = self.voxels(max_points=max_voxels) if max_voxels else {"n": int(self._occupied().size), "cell_m": self.cell_m}
        if not max_voxels:
            vox.update({"size_m": list(self.size_m), "origin": None if self._origin is None else [_r(v) for v in self._origin]})
        return {"t0": _r(self.t_first, 3), "t1": _r(self.t_last, 3), "frame_id": "odom",
                "units": "metres, radians, seconds (host receipt time)",
                "caveats": "positions are odometry-frame estimates; places are free-space clusters, not rooms; "
                           "labels, identities and postures are model guesses",
                "pose": None if self.pose_now is None else [_r(v, 3) for v in self.pose_now],
                "places": self.places(include_cells=True), "entities": entities,
                "events": [self._event_dict(e) for e in list(self._events)[-max(0, int(max_events_out)):]],
                "voxels": vox, "counts": {"entities": len(self._entities), "events": len(self._events), **self.stats}}

    # ---- Elasticsearch -----------------------------------------------------------------------
    @_locked
    def to_elastic_documents(self, since=None, run_id=None) -> list:
        """Stored sightings and events with t > `since` as documents for the strict `annie-spacetime` mapping:
        exactly `ELASTIC_KEYS`, nothing else. The place goes into `text` because the mapping has no place field.
        Timestamps/ids match `memory/elastic.py`, so re-indexing a replay overwrites instead of duplicating."""
        since = _fnum(since)
        run_id = self.run_id if run_id is None else str(run_id)
        docs = []
        for ent in self._entities.values():
            for s in ent.sightings:
                if since is not None and s[_T] <= since:
                    continue
                where = self._place_phrase(*self._place_lookup(s[_X], s[_Y]))
                lying = " lying down" if s[_POSTURE] == "lying" else ""
                docs.append((s[_T], {"@timestamp": _iso(s[_T]), "kind": "person" if ent.kind == "person" else "seen",
                                     "run_id": run_id, "x": float(s[_X]), "y": float(s[_Y]), "z": float(s[_Z]),
                                     "label": ent.label, "identity": ent.identity, "posture": s[_POSTURE],
                                     "track_id": ent.track_id, "text": f"{ent.name} seen{lying} {where}"}))
        for e in self._events:
            if since is not None and e["t"] <= since:
                continue
            where = self._place_phrase(*self._place_lookup(e["x"], e["y"]))
            docs.append((e["t"], {"@timestamp": _iso(e["t"]), "kind": e["kind"], "run_id": run_id, "x": float(e["x"]),
                                  "y": float(e["y"]), "z": 0.0, "label": e["kind"], "identity": None, "posture": None,
                                  "track_id": None, "text": f"{e['text'] or e['kind']} {where}"}))
        docs.sort(key=lambda d: d[0])
        return [d for _, d in docs]

    # ---- bulk ingest / builders --------------------------------------------------------------
    @_locked
    def ingest_all(self, data) -> dict:
        """Live path: feed a `SpacetimeRecorder.snapshot()` (or `.latest()`) dict. Records are replayed in time
        order, and anything at or before what a previous call already ingested is skipped per stream, so
        overlapping or retried snapshots are safe. Returns how many records of each stream were new."""
        if not isinstance(data, dict):
            return {"pose": 0, "obstacles": 0, "people": 0, "event": 0}
        items = []  # (t, order, stream, payload)
        poses = list(data.get("poses") or ())
        if data.get("pose"):
            poses.append(data["pose"])
        for p in poses:
            if isinstance(p, (list, tuple)) and len(p) >= 4:
                items.append((_fnum(p[0]), 0, "pose", p))
        frames = list(data.get("frames") or ())
        if isinstance(data.get("frame"), dict):
            frames.append(data["frame"])
        for f in frames:
            if isinstance(f, dict):
                items.append((_fnum(f.get("t")), 1, "obstacles", f))
        for p in data.get("people") or ():
            if isinstance(p, dict):
                items.append((_fnum(p.get("t", data.get("people_t"))), 2, "people", p))
        for e in data.get("events") or ():
            if isinstance(e, dict):
                items.append((_fnum(e.get("t")), 3, "event", e))
        marks, counts = dict(self._watermark), {"pose": 0, "obstacles": 0, "people": 0, "event": 0}
        for t, _, stream, rec in sorted((i for i in items if i[0] is not None), key=lambda i: (i[0], i[1])):
            if marks[stream] is not None and t <= marks[stream]:
                continue
            if stream == "pose":
                self.ingest_pose(t, rec[1], rec[2], rec[3])
            elif stream == "obstacles":
                self.ingest_obstacles(t, rec.get("points"))
            elif stream == "people":
                self.ingest_sighting(t, rec)
            else:
                self.ingest_event(t, rec.get("x"), rec.get("y"), rec.get("kind"), rec.get("text"))
            counts[stream] += 1
            self._watermark[stream] = t if self._watermark[stream] is None else max(self._watermark[stream], t)
        return counts

    @classmethod
    def from_recorder(cls, recorder, **kwargs) -> "SpacetimeGraph":
        """Replay everything a live `SpacetimeRecorder` still holds (its own window, un-thinned)."""
        graph = cls(**kwargs)
        big = 10 ** 9
        graph.ingest_all(recorder.snapshot(max_frames_out=big, max_poses_out=big, max_people_out=big))
        return graph

    @classmethod
    def from_jsonl(cls, path, **kwargs) -> "SpacetimeGraph":
        """Replay a recorder JSONL run log straight into a graph (the WHOLE file, not just the recorder's last
        15 minutes). Malformed lines are skipped and counted in `.skipped_lines`."""
        graph = cls(**kwargs)
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    d = json.loads(raw)
                    kind = d["type"]
                    if kind == "pose":
                        ok = graph.ingest_pose(d["t"], d["x"], d["y"], d.get("yaw", 0.0))
                    elif kind == "obstacles":
                        ok = graph.ingest_obstacles(d["t"], d["points"])
                    elif kind == "people":
                        ok = isinstance(d["people"], list) and _fnum(d["t"]) is not None
                        graph.ingest_people(d["t"], d["people"] if ok else ())
                    elif kind == "event":
                        graph.ingest_event(d["t"], d.get("x"), d.get("y"), d.get("kind"), d.get("text"))
                        ok = _fnum(d["t"]) is not None  # a repeated identical event is valid, just not stored twice
                    else:
                        ok = False
                except (ValueError, KeyError, TypeError):
                    ok = False
                graph.skipped_lines += 0 if ok else 1
        return graph
