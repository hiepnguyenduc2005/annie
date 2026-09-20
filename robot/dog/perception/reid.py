"""The same person again: names that survive tracker-id churn and restarts.

ByteTrack hands out a new track id every time someone leaves the frame, is occluded or the dog turns away, and
a relaunch forgets everything. `PersonReid` keeps one name per person across that, from three kinds of
evidence, strongest first:

  face          an enrolled, consenting person (`PeopleDirectory` / `face_id.FaceIndex`). Either the tracker
                already attached the match, or this module asks the index itself: one lookup per track every
                `face_every_ms`, and never more than one per `face_every_ms / 3` overall, however many new track
                ids the tracker invents.
  clothing      the BRIDGE between face sightings: a `ClothingSignature` (torso hue/saturation histogram, mean
                colour of upper and lower body, a rough torso/leg proportion) learned the first time a face (or
                the family's shirt-colour rule) confirms a name, then matched by nearest neighbour when the
                face is turned away, too small or too dark.
  place         a PRIOR, never evidence on its own: a name last seen from within `PLACE_RADIUS_M` of where the
                dog stands now, less than `PLACE_MAX_AGE_S` ago, is `PLACE_BONUS` easier to match by clothing.

Unknown regulars become "Guest 1", "Guest 2", ... after `guest_min_sightings` consistent sightings that match
nobody. GUESTS ARE CLOTHING + PLACE ONLY. A guest never consented to anything, so this module never stores
or enrols a face for one, never keeps an image, and forgets the clothing vector `forget_after_s` after the last
sighting. (The one face computation an unnamed person is subject to is the same transient "is this an enrolled
person?" check the tracker already makes; its embedding is compared and discarded.) `.data/faces/guests.json`
(git-ignored, 0600) holds appearance vectors, counters, timestamps and the dog's last position only. Permanent naming of a
guest is an enrolment and goes through the family app with that person's agreement. An operator may instead
assign a session-only demo name to visible upper/lower clothing; that is not face enrollment or identification.

None of this is an identification. Clothing says "dressed like the person I saw a minute ago": two people in
similar dark clothes are one "person" to it, and a change of jacket is a new one. Every identity carries its
`method` so a consumer can tell a face match from a clothing bridge; nothing here may gate safety behaviour or
access on a name.

cv2 and numpy are imported only where pixels are touched, so the table, persistence and `guests()` work in an
environment without them (the family app listing people).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path

from robot.dog.perception.target_id import HUE_BANDS, colour_fraction, torso_region

H_BINS, S_BINS = 8, 4
HIST_LEN = H_BINS * S_BINS
LOW_SATURATION = 48  # below this hue is noise: those pixels are binned by brightness instead (black/grey/white)
MIN_REGION_PX = 6
GUESTS_FILENAME = "guests.json"
GUEST_RE = re.compile(r"^Guest (\d+)$")
PLACE_RADIUS_M, PLACE_MAX_AGE_S, PLACE_BONUS = 1.5, 600.0, 0.08
SHIRT_MIN_FRACTION = 0.35  # same bar as TargetIdentifier
CANDIDATE_FORGET_S = 60.0  # an unnamed appearance not seen again within this never becomes a guest
MAX_CANDIDATES, MAX_GUESTS = 24, 40
# weights of the distance terms; a term missing on either side drops out and the rest are renormalised
W_HIST, W_UPPER, W_LOWER, W_RATIO = 0.55, 0.20, 0.20, 0.05


def _kp_ok(track, indices, kp_conf_min=0.3):
    kps, kpc = track.get("keypoints") or [], track.get("kp_conf") or []
    return all(i < len(kps) and i < len(kpc) and kpc[i] >= kp_conf_min for i in indices)


def lower_region(track, frame_w, frame_h):
    """(x1, y1, x2, y2) of the legs: hips->ankles, else hips->knees, else the box's lower band; None if too small."""
    x1, y1, x2, y2 = (float(v) for v in track["box"])
    kps = track.get("keypoints") or []
    region = None
    for low in ((15, 16), (13, 14)):
        if _kp_ok(track, (11, 12) + low):
            xs = [kps[i][0] for i in (11, 12) + low]
            top = max(kps[11][1], kps[12][1])
            bottom = min(kps[low[0]][1], kps[low[1]][1])
            pad = 0.1 * max(1.0, max(xs) - min(xs))
            region = (min(xs) - pad, top, max(xs) + pad, bottom)
            break
    if region is None:
        h, w = y2 - y1, x2 - x1
        region = (x1 + 0.25 * w, y1 + 0.6 * h, x2 - 0.25 * w, y1 + 0.9 * h)
    rx1, ry1 = max(0.0, region[0]), max(0.0, region[1])
    rx2, ry2 = min(float(frame_w), region[2]), min(float(frame_h), region[3])
    if rx2 - rx1 < MIN_REGION_PX or ry2 - ry1 < MIN_REGION_PX:
        return None
    return int(rx1), int(ry1), int(rx2), int(ry2)


def _proportion(track):
    """Torso length / (torso + leg length) from shoulders, hips and ankles; None without them. Roughly constant
    for one standing person at any distance, unlike the box height."""
    if not _kp_ok(track, (5, 6, 11, 12, 15, 16)):
        return None
    kps = track["keypoints"]
    shoulder = (kps[5][1] + kps[6][1]) / 2.0
    hip = (kps[11][1] + kps[12][1]) / 2.0
    ankle = (kps[15][1] + kps[16][1]) / 2.0
    torso, legs = hip - shoulder, ankle - hip
    if torso <= 2 or legs <= 2:
        return None
    return round(torso / (torso + legs), 4)


def _sample(img_bgr, region, max_side=40):
    """The region's pixels, strided down to at most ~max_side per axis (keeps the signature under a millisecond)."""
    x1, y1, x2, y2 = region
    crop = img_bgr[y1:y2, x1:x2]
    step_y, step_x = max(1, crop.shape[0] // max_side), max(1, crop.shape[1] // max_side)
    return crop[::step_y, ::step_x]


class ClothingSignature:
    """A compact appearance vector for one person in one frame. No pixels are kept.

    `hist`   32 numbers summing to 1: 8 hue x 4 saturation bins over the torso, lightly smoothed across
             neighbouring bins. Hue is shifted half a bin so red (which wraps around 0) lands in one bin; pixels
             too grey to have a hue are binned by brightness in the lowest-saturation column, so black, grey and
             white clothes differ too.
    `upper`, `lower`   mean colour of torso and legs in OpenCV Lab scaled to 0..1 (`lower` is None when the legs
             are out of frame, which is common from a dog's-eye camera up close).
    `ratio`  torso / (torso + legs) from keypoints, None when shoulders, hips or ankles are not confident.
    """

    __slots__ = ("hist", "upper", "lower", "ratio")

    def __init__(self, hist, upper, lower=None, ratio=None):
        self.hist, self.upper, self.lower, self.ratio = list(hist), list(upper), (list(lower) if lower else None), ratio

    @classmethod
    def from_track(cls, img_bgr, track):
        """Signature of `track` (box + optional keypoints/kp_conf) in a BGR frame, or None when the torso is not
        measurable (box off-frame or a few pixels wide)."""
        import cv2
        import numpy as np

        if img_bgr is None or getattr(img_bgr, "ndim", 0) != 3 or not track.get("box"):
            return None
        frame_h, frame_w = img_bgr.shape[:2]
        torso = torso_region(track, frame_w, frame_h)
        if torso is None:
            return None
        crop = _sample(img_bgr, torso)
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
        h, s, v = hsv[:, 0], hsv[:, 1], hsv[:, 2]
        h_bin = ((h + 180 // (2 * H_BINS)) % 180) * H_BINS // 180
        s_bin = np.clip(s * S_BINS // 256, 0, S_BINS - 1)
        grey = s < LOW_SATURATION
        h_bin = np.where(grey, np.clip(v * H_BINS // 256, 0, H_BINS - 1), h_bin)
        s_bin = np.where(grey, 0, np.maximum(s_bin, 1))  # row 0 is reserved for the brightness-binned greys
        grid = np.bincount(h_bin * S_BINS + s_bin, minlength=HIST_LEN).astype(np.float64).reshape(H_BINS, S_BINS)
        grid /= max(1.0, grid.sum())
        # a quarter of each bin leaks to its neighbours, so a shadow that pushes a shirt across a bin edge costs
        # half the histogram term instead of all of it; greys (column 0) never mix with colours
        chroma = 0.5 * grid[:, 1:] + 0.25 * np.roll(grid[:, 1:], 1, axis=0) + 0.25 * np.roll(grid[:, 1:], -1, axis=0)  # hue wraps
        edge = np.pad(chroma, ((0, 0), (1, 1)), mode="edge")
        grid[:, 1:] = 0.5 * chroma + 0.25 * edge[:, :-2] + 0.25 * edge[:, 2:]
        edge = np.pad(grid[:, 0], 1, mode="edge")
        grid[:, 0] = 0.5 * grid[:, 0] + 0.25 * edge[:-2] + 0.25 * edge[2:]
        hist = grid.reshape(-1)

        def mean_lab(region):
            pixels = _sample(img_bgr, region)
            if pixels.size == 0:
                return None
            lab = cv2.cvtColor(np.ascontiguousarray(pixels), cv2.COLOR_BGR2LAB).reshape(-1, 3)
            return [round(float(c) / 255.0, 4) for c in lab.mean(axis=0)]

        legs = lower_region(track, frame_w, frame_h)
        return cls([round(float(x), 5) for x in hist], mean_lab(torso), mean_lab(legs) if legs else None, _proportion(track))

    @staticmethod
    def _colour_distance(a, b):
        # lightness counts half: the same shirt in the hallway and by the window should stay the same shirt
        d = math.sqrt(0.25 * (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)
        return min(1.0, d / 0.35)  # 0.35 ~ saturated red vs saturated blue in scaled Lab

    @staticmethod
    def distance(a: "ClothingSignature", b: "ClothingSignature") -> float:
        """0 = same appearance, 1 = nothing in common. A weighted mean of the terms both sides have."""
        terms = [(W_HIST, 0.5 * sum(abs(x - y) for x, y in zip(a.hist, b.hist))),
                 (W_UPPER, ClothingSignature._colour_distance(a.upper, b.upper))]
        if a.lower and b.lower:
            terms.append((W_LOWER, ClothingSignature._colour_distance(a.lower, b.lower)))
        if a.ratio is not None and b.ratio is not None:
            terms.append((W_RATIO, min(1.0, abs(a.ratio - b.ratio) / 0.25)))
        total = sum(w for w, _ in terms)
        return round(max(0.0, min(1.0, sum(w * d for w, d in terms) / total)), 4)

    def blended(self, new: "ClothingSignature", alpha=0.2) -> "ClothingSignature":
        """This signature moved `alpha` of the way to `new` (follows slow lighting drift without jumping)."""
        def mix(old, fresh):
            if old is None or fresh is None:
                return fresh if old is None else old
            return [round((1 - alpha) * o + alpha * f, 5) for o, f in zip(old, fresh)]
        ratio = self.ratio if new.ratio is None else new.ratio if self.ratio is None else round((1 - alpha) * self.ratio + alpha * new.ratio, 4)
        return ClothingSignature(mix(self.hist, new.hist), mix(self.upper, new.upper), mix(self.lower, new.lower), ratio)

    def to_json(self) -> dict:
        return {"hist": self.hist, "upper": self.upper, "lower": self.lower, "ratio": self.ratio}

    @classmethod
    def from_json(cls, data):
        """A signature from saved JSON, or None when the record is not one (a damaged file loses that entry only)."""
        try:
            hist, upper = [float(x) for x in data["hist"]], [float(x) for x in data["upper"]]
            lower = [float(x) for x in data["lower"]] if data.get("lower") else None
            ratio = float(data["ratio"]) if data.get("ratio") is not None else None
        except (KeyError, TypeError, ValueError):
            return None
        if len(hist) != HIST_LEN or len(upper) != 3 or (lower is not None and len(lower) != 3):
            return None
        if not all(math.isfinite(x) for x in hist + upper + (lower or []) + ([ratio] if ratio is not None else [])):
            return None
        return cls(hist, upper, lower, ratio)


def spoken_name(identity) -> str | None:
    """The name to say aloud: an enrolled person's, never "Guest 2" (greet a guest without a name)."""
    if not identity or identity.get("method") == "guest" or GUEST_RE.match(str(identity.get("name") or "")):
        return None
    return identity.get("name") or None


class _Appearance:
    """What one name looks like right now, and when/where the dog last saw it."""

    __slots__ = ("sig", "guest", "source", "created_s", "last_seen_s", "sightings", "xy", "xy_s")

    def __init__(self, sig, *, guest, source, now_s, xy=None):
        self.sig, self.guest, self.source = sig, guest, source
        self.created_s = self.last_seen_s = now_s
        self.sightings = 1
        self.xy, self.xy_s = (xy, now_s) if xy else (None, None)

    def to_json(self):
        return {"sig": self.sig.to_json(), "source": self.source, "created_s": round(self.created_s, 1),
                "last_seen_s": round(self.last_seen_s, 1), "sightings": self.sightings,
                "xy": [round(v, 2) for v in self.xy] if self.xy else None, "xy_s": round(self.xy_s, 1) if self.xy_s else None}

    @classmethod
    def from_json(cls, data, *, guest):
        sig = ClothingSignature.from_json(data.get("sig") or {}) if isinstance(data, dict) else None
        if sig is None:
            return None
        try:
            item = cls(sig, guest=guest, source=str(data.get("source") or ("guest" if guest else "face")),
                       now_s=float(data["last_seen_s"]))
            item.created_s = float(data.get("created_s", item.last_seen_s))
            item.sightings = int(data.get("sightings", 1))
            xy = data.get("xy")
            if isinstance(xy, list) and len(xy) == 2 and data.get("xy_s") is not None:
                item.xy, item.xy_s = (float(xy[0]), float(xy[1])), float(data["xy_s"])
        except (KeyError, TypeError, ValueError):
            return None
        return item


class PersonReid:
    """Names tracks by face, then clothing, then the family's shirt-colour rule, then a stable guest number.

    A drop-in for `TargetIdentifier` in the perception pipeline: `apply(img, tracks)` works, and so does the
    full `apply(img, jpeg, tracks, now_ms, pose_xy)`. With `pose_source` set (a callable returning the dog's
    `(x, y)` or None) the place prior works from the two-argument call too. Shirt-colour rules come from
    `directory.shirt_identities()` (read every frame, so the family app's edits apply at once) plus `shirt_rules`.
    """

    def __init__(self, directory=None, *, face_every_ms=1500, match_face=0.45, match_clothes=0.28,
                 guest_min_sightings=3, forget_after_s=1800, path=None, sighting_gap_ms=500, save_every_s=15.0,
                 pose_source=None, shirt_rules=None, clock=time.time):
        self.directory = directory
        self.face_every_ms, self.match_face, self.match_clothes = int(face_every_ms), float(match_face), float(match_clothes)
        self.guest_min_sightings, self.forget_after_s = max(1, int(guest_min_sightings)), float(forget_after_s)
        self.sighting_gap_ms, self.save_every_s = int(sighting_gap_ms), float(save_every_s)
        self.pose_source, self.clock = pose_source, clock
        # extra [(name, colour)] next to the directory's own, or a callable returning them (patrol's --target)
        self.shirt_rules = shirt_rules
        if path is None:
            path = Path(getattr(directory, "faces_dir", ".data/faces")) / GUESTS_FILENAME
        self.path = Path(path)
        self.lock = threading.RLock()  # apply() runs on the tracker thread, list/forget on the HTTP thread
        self.known: dict[str, _Appearance] = {}
        self.next_guest = 1
        self.face_error: str | None = None  # last face lookup failure (type name only), never raised
        self.matches = {"face": 0, "clothing": 0, "shirt_colour": 0, "guest": 0}
        self._tracks: dict[int, dict] = {}  # track_id -> {"name", "method", "score", "last_ms"}
        self._face_tried: dict[int, int] = {}  # track_id -> now_ms of the last lookup
        self._face_last_ms = -10 ** 12  # any track: id churn must not turn into a face lookup per frame
        self._candidates: list[dict] = []  # unnamed appearances on their way to becoming a guest
        self._dirty, self._saved_s, self._expired_s = False, 0.0, 0.0
        self._selection = {"name": None, "guest": None, "state": "unselected", "needs_selection": True}
        self._selected_sig = None
        self._selected_seen_s = None
        self._visible_guests = {}
        self._load()

    # -- persistence (clothing vectors, counters, timestamps; never an image or a face) -----------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        now_s = self.clock()
        enrolled = self._enrolled_names()
        for section, guest in (("guests", True), ("known", False)):
            records = data.get(section)
            for name, record in (records.items() if isinstance(records, dict) else ()):
                if guest != bool(GUEST_RE.match(str(name))):
                    continue
                if not guest and enrolled is not None and name not in enrolled:
                    continue  # forgotten in the family app while the dog was off
                item = _Appearance.from_json(record, guest=guest)
                if item is not None and now_s - item.last_seen_s <= self.forget_after_s:
                    self.known[str(name)] = item
        numbers = [int(GUEST_RE.match(n).group(1)) for n, a in self.known.items() if a.guest]
        try:
            saved_next = int(data.get("next_guest", 1))
        except (TypeError, ValueError):
            saved_next = 1
        # numbers are never reused while the file lives: "Guest 2" in the memory graph stays that person
        self.next_guest = max([saved_next, 1] + [n + 1 for n in numbers])

    def _enrolled_names(self):
        """Names the directory knows (metadata or faces); None when there is no directory to ask."""
        if self.directory is None:
            return None
        names = set(getattr(self.directory, "meta", None) or {})
        try:
            index = self.directory.index()
            names |= set(index.names()) if index is not None else set()
        except Exception:
            pass
        return names

    def save(self):
        with self.lock:
            payload = {"version": 1, "next_guest": self.next_guest,
                       "guests": {n: a.to_json() for n, a in sorted(self.known.items()) if a.guest},
                       "known": {n: a.to_json() for n, a in sorted(self.known.items()) if not a.guest}}
            self._dirty, self._saved_s = False, self.clock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass  # a read-only disk must not stop the dog naming people this session

    # -- table ------------------------------------------------------------------------------------------------
    def guests(self) -> list[dict]:
        with self.lock:
            return [{"name": n, "guest": True, "sightings": a.sightings, "added_at": a.created_s, "last_seen": a.last_seen_s}
                    for n, a in sorted(self.known.items(), key=lambda kv: int(GUEST_RE.match(kv[0]).group(1)) if kv[1].guest else 0)
                    if a.guest]

    def _selection_lost(self, state):
        self._selection.update(state=state, needs_selection=True)
        name = self._selection["name"]
        self._tracks = {tid: held for tid, held in self._tracks.items() if held["name"] != name}

    def selection_status(self) -> dict:
        """Session-only selection; loss and ambiguity require explicit assignment again."""
        with self.lock:
            if self._selection["state"] == "tracking" and self.clock() - self._selected_seen_s > 2.0:
                self._selection_lost("lost")
            return dict(self._selection)

    def assign_guest(self, guest_name, name="Jeanine", *, now_s=None) -> dict:
        """Assign fresh visible upper/lower clothing without face lookup or enrollment.

        Raises ValueError for an invalid, absent, stale or incomplete guest. No image or
        assignment is persisted; a restart requires operator selection again.
        """
        now_s = self.clock() if now_s is None else float(now_s)
        if not isinstance(guest_name, str) or not GUEST_RE.fullmatch(guest_name):
            raise ValueError("Select an existing Guest N")
        if not isinstance(name, str) or not name.strip() or GUEST_RE.fullmatch(name.strip()):
            raise ValueError("A non-guest name is required")
        if not math.isfinite(now_s):
            raise ValueError("Invalid selection timestamp")
        with self.lock:
            item = self.known.get(guest_name)
            visible = self._visible_guests.get(guest_name)
            if item is None or not item.guest or visible is None:
                raise ValueError("Guest is not currently visible")
            sig, seen_s = visible
            if not 0.0 <= now_s - seen_s <= 2.0:
                raise ValueError("Guest observation is stale")
            if sig is None or sig.lower is None:
                raise ValueError("Both upper and lower clothing must be visible")
            old_name = self._selection["name"]
            name = name.strip()
            self._tracks = {tid: held for tid, held in self._tracks.items()
                            if held["name"] not in (old_name, name, guest_name)}
            self.known.pop(guest_name)
            self.known.pop(name, None)
            self._visible_guests.pop(guest_name)
            self._selected_sig = sig
            self._selected_seen_s = seen_s
            self._selection = {"name": name, "guest": guest_name, "state": "tracking", "needs_selection": False}
            self._dirty = True
            return dict(self._selection)

    def forget(self, name: str) -> bool:
        """Drop a name's clothing signature (a guest disappears entirely); tracks holding the name are released."""
        with self.lock:
            removed = self.known.pop(name, None) is not None
            self._visible_guests.pop(name, None)
            if name in (self._selection["name"], self._selection["guest"]):
                self._selection_lost("lost")
                removed = True
            for tid in [tid for tid, held in self._tracks.items() if held["name"] == name]:
                del self._tracks[tid]
                removed = True
        if removed:
            self.save()
        return removed

    def _expire(self, now_s):
        if now_s - self._expired_s < 5.0:
            return
        self._expired_s = now_s
        for name in [n for n, a in self.known.items() if now_s - a.last_seen_s > self.forget_after_s]:
            del self.known[name]
            self._dirty = True
        self._candidates = [c for c in self._candidates if now_s - c["last_s"] <= CANDIDATE_FORGET_S][-MAX_CANDIDATES:]

    def _observe(self, name, sig, now_s, xy, *, guest=False, source="face", confirmed=False):
        """Record a sighting of `name`. `confirmed` (a face said so) may replace a signature that no longer fits:
        they changed clothes. A clothing match only ever nudges it."""
        item = self.known.get(name)
        if item is None:
            if sig is None:
                return
            item = self.known[name] = _Appearance(sig, guest=guest, source=source, now_s=now_s, xy=xy)
            self._dirty = True
        elif sig is not None:
            d = ClothingSignature.distance(item.sig, sig)
            if d <= self.match_clothes:
                item.sig = item.sig.blended(sig)
            elif confirmed:
                item.sig, item.source = sig, source
            self._dirty = True
        if now_s - item.last_seen_s >= self.sighting_gap_ms / 1000.0:
            item.sightings += 1
        item.last_seen_s = now_s
        if xy:
            item.xy, item.xy_s = xy, now_s
        self._dirty = True

    def _threshold(self, item, now_s, xy, *, same_track=False):
        if same_track:
            return 2 * self.match_clothes  # the tracker says it is the same body; only a clear contradiction unnames it
        if xy and item.xy and item.xy_s is not None and 0.0 <= now_s - item.xy_s <= PLACE_MAX_AGE_S \
                and math.dist(xy, item.xy) <= PLACE_RADIUS_M:
            return self.match_clothes + PLACE_BONUS  # seen right here a moment ago: a weaker resemblance is enough
        return self.match_clothes

    # -- evidence ---------------------------------------------------------------------------------------------
    def _face_index(self):
        if self.directory is None:
            return None
        try:
            index = self.directory.index()
            return index if index is not None and index.names() else None
        except Exception:
            return None

    def _face_lookup(self, index, img_bgr, jpeg, track):
        """One face lookup for a track. With the frame's JPEG the index crops the box itself; on the live path
        there is only a BGR array, so the person's head-and-shoulders crop is encoded here."""
        try:
            if isinstance(jpeg, (bytes, bytearray)):
                found = index.identify(bytes(jpeg), track["box"])
            else:
                import cv2
                x1, y1, x2, y2 = (float(v) for v in track["box"])
                h, w = img_bgr.shape[:2]
                pad = 0.1 * (x2 - x1)
                cx1, cx2 = max(0, int(x1 - pad)), min(w, int(math.ceil(x2 + pad)))
                head = 1.0 if (x2 - x1) > (y2 - y1) else 0.5  # lying: the head can be at either end of the box
                cy1, cy2 = max(0, int(y1)), min(h, int(math.ceil(y1 + head * (y2 - y1))))
                if cx2 - cx1 < 20 or cy2 - cy1 < 20:
                    return None
                ok, buf = cv2.imencode(".jpg", img_bgr[cy1:cy2, cx1:cx2], [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                if not ok:
                    return None
                found = index.identify(buf.tobytes(), None)
        except Exception as exc:  # noqa: BLE001 - a face failure must never stop clothing matching or tracking
            self.face_error = type(exc).__name__
            return None
        if not found or float(found.get("score", 0.0)) < self.match_face:
            return None
        return {"name": str(found["name"]), "score": round(float(found["score"]), 4)}

    def _rules(self):
        rules = []
        for source in (getattr(self.directory, "shirt_identities", None), self.shirt_rules):
            try:
                found = source() if callable(source) else (source or [])
                rules += [(str(n), c) for n, c in found if n and c in HUE_BANDS and (str(n), c) not in rules]
            except Exception:
                continue
        return rules

    def _shirt(self, img_bgr, track, rules):
        if not rules:
            return None
        region = torso_region(track, img_bgr.shape[1], img_bgr.shape[0])
        if region is None:
            return None
        best = max(((colour_fraction(img_bgr, region, colour), name) for name, colour in rules), key=lambda fn: fn[0])
        track["shirt_colour_fraction"] = round(best[0], 3)
        if best[0] < SHIRT_MIN_FRACTION:
            return None
        return {"name": best[1], "score": round(best[0], 3)}

    def _candidate(self, sig, now_ms, now_s):
        """Count a sighting of an appearance nobody claims; returns True when it has earned a guest number."""
        best, best_d = None, self.match_clothes
        for cand in self._candidates:
            d = ClothingSignature.distance(cand["sig"], sig)
            if d <= best_d:
                best, best_d = cand, d
        if best is None:
            best = {"sig": sig, "n": 1, "last_ms": now_ms, "last_s": now_s}
            self._candidates.append(best)
        else:
            best["sig"], best["last_s"] = best["sig"].blended(sig), now_s
            if now_ms - best["last_ms"] >= self.sighting_gap_ms:  # 3 consecutive frames are one sighting, not three
                best["n"], best["last_ms"] = best["n"] + 1, now_ms
        if best["n"] >= self.guest_min_sightings:
            self._candidates.remove(best)
            return True
        return False

    # -- per frame --------------------------------------------------------------------------------------------
    def apply(self, img_bgr, jpeg=None, tracks=None, now_ms=None, pose_xy=None):
        """Set `identity = {"name", "score", "method"}` (or None) on every track, in place; returns the tracks.

        `method` is "face", "clothing", "shirt_colour" or "guest". `score` is the face cosine, the shirt colour
        fraction, or `1 - clothing distance`; none of them is a probability.
        """
        if tracks is None and isinstance(jpeg, (list, tuple)):
            jpeg, tracks = None, jpeg  # the pipeline's identifier.apply(img, tracks)
        tracks = tracks if tracks is not None else []
        now_ms = int(self.clock() * 1000) if now_ms is None else int(now_ms)
        now_s = now_ms / 1000.0
        if pose_xy is None and self.pose_source is not None:
            try:
                pose_xy = self.pose_source()
            except Exception:
                pose_xy = None
        xy = (float(pose_xy[0]), float(pose_xy[1])) if pose_xy is not None and len(pose_xy) >= 2 else None

        with self.lock:
            self._expire(now_s)
            rules = [(n, c) for n, c in self._rules() if n != self._selection["name"]]
            ruled = {n for n, _ in rules}
            for name in [n for n, a in self.known.items() if a.source == "shirt_colour" and n not in ruled]:
                del self.known[name]  # the rule was renamed or removed in the app: what it taught goes with it
                self._dirty = True
            sigs = [ClothingSignature.from_track(img_bgr, t) for t in tracks]
            decided: dict[int, dict] = {}  # index in `tracks` -> identity
            taken: set[str] = {self._selection["name"]} if self._selection["name"] else set()
            selected_match = None
            if self._selection["state"] == "tracking":
                if now_s - self._selected_seen_s > 2.0:
                    self._selection_lost("lost")
                else:
                    matches = [(i, ClothingSignature.distance(self._selected_sig, sig))
                               for i, sig in enumerate(sigs) if sig is not None]
                    matches = [(i, d) for i, d in matches if d <= self.match_clothes]
                    if len(matches) > 1:
                        self._selection_lost("ambiguous")
                    elif len(matches) == 1 and sigs[matches[0][0]].lower is not None:
                        selected_match = matches[0]
                        self._selected_seen_s = now_s
                        self._selected_sig = self._selected_sig.blended(sigs[selected_match[0]])

            def decide(i, name, score, method):
                decided[i] = {"name": name, "score": score, "method": method}
                taken.add(name)

            # 1. faces. The tracker's own match first, then at most one lookup of ours, longest-waiting track first.
            index = self._face_index()
            due = []
            for i, t in enumerate(tracks):
                if selected_match is not None and i == selected_match[0]:
                    continue
                existing, tid = t.get("identity"), t.get("track_id")
                if existing and existing.get("name") and existing.get("method") in (None, "face") \
                        and existing["name"] not in taken:
                    decide(i, str(existing["name"]), round(float(existing.get("score", 0.0)), 4), "face")
                    continue
                held = self._tracks.get(tid) if tid is not None else None
                if held and held["method"] == "face" and held["name"] not in taken:
                    item = self.known.get(held["name"])
                    # fresh = same track and still dressed like the person the face confirmed; a ByteTrack id
                    # switch onto someone else shows up as a clothing contradiction and the name is dropped
                    if item is None or sigs[i] is None or ClothingSignature.distance(item.sig, sigs[i]) <= 2 * self.match_clothes:
                        decide(i, held["name"], held["score"], "face")
                        continue
                    del self._tracks[tid]
                if index is not None and tid is not None and now_ms - self._face_tried.get(tid, -10 ** 12) >= self.face_every_ms:
                    due.append((self._face_tried.get(tid, -10 ** 12), i))
            if due and now_ms - self._face_last_ms >= self.face_every_ms // 3:
                _, i = min(due)
                self._face_tried[tracks[i]["track_id"]] = self._face_last_ms = now_ms
                found = self._face_lookup(index, img_bgr, jpeg, tracks[i])
                if found and found["name"] not in taken:
                    decide(i, found["name"], found["score"], "face")
            for i, ident in list(decided.items()):
                name = ident["name"]
                if sigs[i] is not None:  # a guest who turns out to be family stops being a guest
                    for guest in [n for n, a in self.known.items() if a.guest
                                  and ClothingSignature.distance(a.sig, sigs[i]) <= self.match_clothes]:
                        del self.known[guest]
                        for tid in [tid for tid, held in self._tracks.items() if held["name"] == guest]:
                            del self._tracks[tid]
                self._observe(name, sigs[i], now_s, xy, source="face", confirmed=True)

            if selected_match is not None:
                i, distance = selected_match
                decide(i, self._selection["name"], round(1.0 - distance, 4), "clothing")

            # 2. clothing, nearest first so the better-fitting track gets a contested name
            pairs = []
            for i, sig in enumerate(sigs):
                if i in decided or sig is None:
                    continue
                held = self._tracks.get(tracks[i].get("track_id"))
                for name, item in self.known.items():
                    if name == self._selection["name"]:
                        continue
                    d = ClothingSignature.distance(item.sig, sig)
                    if d <= self._threshold(item, now_s, xy, same_track=bool(held and held["name"] == name)):
                        pairs.append((d, i, name))
            clothing: dict[int, tuple[float, str]] = {}
            for d, i, name in sorted(pairs):
                if i not in clothing and name not in taken and name not in {n for _, n in clothing.values()}:
                    clothing[i] = (d, name)

            for i, t in enumerate(tracks):
                if i in decided:
                    continue
                match = clothing.get(i)
                shirt = None
                if match is None or self.known[match[1]].guest or self.known[match[1]].source == "shirt_colour":
                    shirt = self._shirt(img_bgr, t, rules)  # the family's rule outranks a learned guest, not a learned face
                    if shirt and shirt["name"] in taken:
                        shirt = None
                if shirt:
                    decide(i, shirt["name"], shirt["score"], "shirt_colour")
                    self._observe(shirt["name"], sigs[i], now_s, xy, source="shirt_colour", confirmed=True)
                elif match and match[1] not in taken:
                    d, name = match
                    item = self.known[name]
                    decide(i, name, round(1.0 - d, 4), "guest" if item.guest else "clothing")
                    self._observe(name, sigs[i], now_s, xy, guest=item.guest, source=item.source)
                elif sigs[i] is not None and self._candidate(sigs[i], now_ms, now_s) \
                        and sum(1 for a in self.known.values() if a.guest) < MAX_GUESTS:
                    name = f"Guest {self.next_guest}"
                    self.next_guest += 1
                    self.known[name] = _Appearance(sigs[i], guest=True, source="guest", now_s=now_s, xy=xy)
                    self.known[name].sightings = self.guest_min_sightings
                    decide(i, name, 1.0, "guest")
                    self._dirty, self._saved_s = True, 0.0  # a new guest number is written now, not in 15 s

            self._visible_guests = {ident["name"]: (sigs[i], now_s) for i, ident in decided.items()
                                    if ident["method"] == "guest"}
            for i, t in enumerate(tracks):
                ident = decided.get(i)
                t["identity"] = ident
                tid = t.get("track_id")
                if tid is None:
                    continue
                if ident:
                    self.matches[ident["method"]] += 1
                    self._tracks[tid] = {"name": ident["name"], "method": ident["method"], "score": ident["score"], "last_ms": now_ms}
                elif tid in self._tracks:
                    self._tracks[tid]["last_ms"] = now_ms
            for tid in [tid for tid, held in self._tracks.items() if now_ms - held["last_ms"] > 10_000]:
                del self._tracks[tid]
            for tid in [tid for tid, tried in self._face_tried.items() if now_ms - tried > 60_000]:
                del self._face_tried[tid]
            save_due = self._dirty and now_s - self._saved_s >= self.save_every_s
        if save_due:
            self.save()
        return tracks
