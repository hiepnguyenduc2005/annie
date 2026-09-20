"""Resident-level check-in dedupe: one lying resident is one check-in, not one track id.

The tracker can hand the same resident a new id after a few seconds out of view, and
GreetPolicy's per-id cooldown then asks "are you alright?" twice. A blind global
cooldown is worse: a second resident actually down would be silently swallowed.
This module keeps small, bounded per-resident evidence instead:

* identity   any explicitly asserted non-guest name on the track (today the
             tracker/reid produce face matches and the demo's shirt_colour rule;
             any future method that asserts a name counts the same way). Guest
             enrolments (identity.method "guest" or a "Guest 1"-style alias) are
             clothing and place only, never a stable identity here: Guest 1 on a
             new track id may be a different person in similar clothes, so guests
             are treated as unnamed.
* place      an estimated world position derived from the observation pose and the
             detection box, with an uncertainty radius supplied by the caller.

Conservative-by-default geometry (asking twice is always the safe failure):

* suppression  a lying detection is suppressed only when it agrees with a recently
               asked resident: the same stable name, or (either side unnamed) a place
               within the combined uncertainty CAPPED at MAX_SUPPRESS_SPAN_M. The cap
               keeps a wide far-range estimate from swallowing a distinct nearby
               person; the cost of under-suppression is a repeat question, never a
               missed resident.
* concurrency two simultaneously visible lying tracks are distinct people even
               when their places are within the span: while the record's original
               track id is itself still visible lying, a different current track id
               always asks. Churn (the original id gone, one resident re-seen on a
               new id) still suppresses.
* resolution   an episode clears only on positive evidence: an explicit
               upright sighting of the same stable name, or (unnamed) an
               upright sighting within RESOLVE_RADIUS_M of the recorded place. The
               resolve span deliberately ignores the uncertainty radii -- being close
               enough to *see someone up* is stricter than being unsure where
               *someone* is. The tracker's transient "unknown" posture never
               resolves.

Pure and synchronous: no I/O, no threads, no model, no hardware.
"""
from __future__ import annotations

import re

EPISODE_WINDOW_S = 180.0      # asked-state expiry: after this quiet, the same spot asks again
MATCH_RADIUS_M = 0.6          # default place-estimate uncertainty when the caller has none
MAX_SUPPRESS_SPAN_M = 1.2     # cap on the combined uncertainty: beyond this, distinct people
RESOLVE_RADIUS_M = 0.5        # unnamed upright sighting must be this near the recorded place
GUEST_NAME_RE = re.compile(r"^guest \d+$")
RESOLVE_POSTURES = ("upright",)  # tracker vocabulary is upright/lying/unknown (posture.py); unknown never resolves
MAX_EPISODES = 32             # bounded memory: recent asked records in total


def norm_name(track) -> str | None:
    """The track's stable identity name (lowercased), or None when unnamed or a guest alias."""
    identity = track.get("identity") or {}
    if identity.get("method") == "guest":
        return None
    name = str(identity.get("name") or "").strip().lower()
    if not name or GUEST_NAME_RE.match(name):
        return None
    return name


class ResidentPolicy:
    """Ask-once-per-episode decisions for the patrol's GreetPolicy. See module docstring."""

    def __init__(self, *, episode_window_s: float = EPISODE_WINDOW_S, match_radius_m: float = MATCH_RADIUS_M):
        self.episode_window_s = float(episode_window_s)
        self.match_radius_m = float(match_radius_m)
        self._asked: list[dict] = []  # {"name", "place", "radius", "asked_s"}

    # ------------------------------------------------------------------ internals
    def _active(self, now_s):
        self._asked = [r for r in self._asked if now_s - r["asked_s"] <= self.episode_window_s]
        return self._asked

    @staticmethod
    def _suppress_span(record, radius_m) -> float:
        return min(record["radius"] + radius_m, MAX_SUPPRESS_SPAN_M)

    def _same_person(self, record, name, place, radius_m) -> bool:
        if name is not None and record["name"] is not None:
            return name == record["name"]  # stable names decide, wherever they are
        if place is None or record["place"] is None:
            return False  # without a shared name or geometry, never suppress
        return self._dist(place, record["place"]) <= self._suppress_span(record, radius_m)

    @staticmethod
    def _dist(a, b) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    # ------------------------------------------------------------------ decisions
    def should_ask(self, track, place, *, now_s, radius_m: float | None = None,
                   visible_lying_tids=None) -> bool:
        """True when this lying detection is a resident episode that has not been asked about.

        visible_lying_tids is the set of track ids currently seen lying. While an
        already-asked record's own track id is still visible lying, a different
        current track id is treated as a distinct person even at the same place.
        """
        name, radius = norm_name(track), self.match_radius_m if radius_m is None else float(radius_m)
        tid = track.get("track_id")
        for rec in self._active(now_s):
            if tid is not None and rec.get("tid") is not None and rec["tid"] != tid                     and rec["tid"] in (visible_lying_tids or ()):
                continue  # concurrent distinct tracks: never suppress this one
            if self._same_person(rec, name, place, radius):
                return False
        return True

    def mark_asked(self, track, place, *, now_s, radius_m: float | None = None) -> None:
        """Record that this lying detection was asked about (call when the check-in starts)."""
        self._active(now_s)
        self._asked.append({"name": norm_name(track), "tid": track.get("track_id"),
                            "place": None if place is None else (float(place[0]), float(place[1])),
                            "radius": self.match_radius_m if radius_m is None else float(radius_m),
                            "asked_s": float(now_s)})
        del self._asked[:-MAX_EPISODES]

    # ------------------------------------------------------------------ recovery
    def observe(self, tracks, place_by_tid, *, now_s) -> None:
        """Feed ongoing tracker output: positive recovery evidence resolves the matching episode."""
        up = [(norm_name(t), place_by_tid.get(t.get("track_id")))
              for t in tracks if t.get("posture") in RESOLVE_POSTURES]
        self._asked = [rec for rec in self._active(now_s) if not any(self._resolved(rec, name, place)
                                                                     for name, place in up)]

    def _resolved(self, record, name, place) -> bool:
        if name is not None and record["name"] is not None:
            return name == record["name"]  # the named resident is verifiably up: episode over
        if place is None or record["place"] is None:
            return False
        # Deliberately tighter than suppression, and independent of the current track's
        # uncertainty: an upright sighting clears an unnamed episode only when it stands
        # where the resident was seen lying (allowing for half the recorded estimate's
        # error, capped), not merely "somewhere nearby".
        span = min(RESOLVE_RADIUS_M + 0.5 * record["radius"], MAX_SUPPRESS_SPAN_M)
        return self._dist(place, record["place"]) <= span
