"""Deterministic search progress for one mission on one map.

Which waypoints the dog has reached, which it inspected and found empty, and
where a person was last seen. Updated only from ingested perceptions and command
receipts with caller-supplied timestamps: no model, no network, no re-dated or
synthesized capture. It holds no resident identity; a sighting records that a
person was visible in one captured frame, not who it was.

Events are transitions, not per-frame echoes, so callers can re-plan on them.
"""
from __future__ import annotations

import math

UNVISITED = "unvisited"
VISITED = "visited"
INSPECTED_EMPTY = "inspected_empty"
PERSON_SEEN = "person_seen"

SIGHTING_MIN_CONFIDENCE = 0.5
TERMINAL_MEMORY = 64
POSE_KEYS = ("x", "y", "yaw", "map_id")
SIGHTING_KEYS = ("frame_id", "ts", "posture", "location", "confidence")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class MissionState:
    """Per-waypoint search status, last sighting, and current goto destination."""

    def __init__(self, map_id: str, waypoints: list[dict], *, inspected_ttl_ms: int = 120_000, arrive_radius_m: float = 1.0):
        self.map_id = map_id
        self.inspected_ttl_ms = int(inspected_ttl_ms)
        self.arrive_radius_m = float(arrive_radius_m)
        self._xy_by_id = {w["id"]: (float(w["x"]), float(w["y"])) for w in waypoints}
        self._status = {wid: UNVISITED for wid in self._xy_by_id}
        self._stamp_ms = {wid: None for wid in self._xy_by_id}
        self.last_sighting = None
        self.destination = None
        self._pose_xy = None      # last measured same-map position, for summary()
        self._last_view = None    # (waypoint_id, person_visible) of the previous in-radius view
        self._terminal = []       # recent terminal command ids; retried receipts are no-ops

    def observe(self, perception: dict, *, now_ms: int) -> list[str]:
        """Fold one ingested perception into search progress."""
        perception = perception if isinstance(perception, dict) else {}
        pose = perception.get("pose")
        xy = self._xy(pose)
        if xy is None:
            return []  # other map or unusable pose: never evidence about this mission
        self._pose_xy = xy
        wid = self._nearest(xy, self._xy_by_id)
        if wid is None or math.dist(xy, self._xy_by_id[wid]) > self.arrive_radius_m:
            self._last_view = None
            return []
        if perception.get("person") is True:
            confidence = perception.get("confidence")
            if not _number(confidence) or confidence < SIGHTING_MIN_CONFIDENCE:
                return []  # neither a sighting nor evidence of absence
            new = self._status[wid] != PERSON_SEEN or self._last_view != (wid, True)
            self._mark(wid, PERSON_SEEN, now_ms)
            self._last_view = (wid, True)
            self.last_sighting = {**{key: perception.get(key) for key in SIGHTING_KEYS}, "waypoint_id": wid,
                                  "pose": {k: pose[k] for k in POSE_KEYS if k in pose}}
            return [PERSON_SEEN] if new else []
        self._last_view = (wid, False)
        if self._status[wid] == PERSON_SEEN:
            return []  # one empty view does not erase a sighting
        new = self._effective(wid, now_ms) != INSPECTED_EMPTY
        self._mark(wid, INSPECTED_EMPTY, now_ms)
        return [INSPECTED_EMPTY] if new else []

    def command_update(self, outcome: dict, *, now_ms: int) -> list[str]:
        """Fold one command receipt into destination and visited state."""
        outcome = outcome if isinstance(outcome, dict) else {}
        status, cid = outcome.get("status"), outcome.get("command_id")
        if cid is not None and cid in self._terminal:
            return []  # retried or late receipt for a finished command
        if status in ("completed", "failed") and cid is not None:
            self._terminal = (self._terminal + [cid])[-TERMINAL_MEMORY:]
        wid = outcome.get("waypoint")
        if outcome.get("cmd") == "goto" and isinstance(wid, str):
            if status in ("accepted", "executing"):
                self.destination = {"waypoint_id": wid, "command_id": cid}
            elif status in ("completed", "failed") and self._is_destination(wid, cid):
                self.destination = None
            if status == "completed":
                if self._status.get(wid) == UNVISITED:
                    self._mark(wid, VISITED, now_ms)  # never downgrades stronger evidence
                return ["arrived"]
        return ["command_failed"] if status == "failed" else []

    def next_search_target(self, pose: dict, *, now_ms: int) -> str | None:
        """Nearest unvisited waypoint, else the oldest expired inspection, else None."""
        return self._target(self._xy(pose), now_ms)

    def summary(self, *, now_ms: int) -> dict:
        """Planner-facing snapshot; callers may mutate the result freely."""
        sighting = self.last_sighting
        return {
            "waypoints": [{"id": wid, "status": self._effective(wid, now_ms), "age_s": self._age_s(wid, now_ms)}
                          for wid in self._xy_by_id],
            "last_sighting": None if sighting is None else {**sighting, "pose": dict(sighting["pose"])},
            "destination": None if self.destination is None else dict(self.destination),
            "suggested_target": self._target(self._pose_xy, now_ms),
        }

    def should_replan(self, *, now_ms: int, events: list[str], last_plan_ms: int | None, interval_ms: int = 30_000) -> bool:
        """Plan on any event, on the first call, or when the timer elapses."""
        return bool(events) or last_plan_ms is None or now_ms - last_plan_ms >= interval_ms

    def _xy(self, pose):
        if not isinstance(pose, dict) or pose.get("map_id") != self.map_id:
            return None
        x, y = pose.get("x"), pose.get("y")
        return (float(x), float(y)) if _number(x) and _number(y) else None

    def _nearest(self, xy, ids):
        """Closest of ids by straight line; list order breaks ties and stands in for no pose."""
        if xy is None:
            return next(iter(ids), None)
        return min(ids, key=lambda wid: math.dist(xy, self._xy_by_id[wid]), default=None)

    def _mark(self, wid, status, now_ms):
        self._status[wid] = status
        self._stamp_ms[wid] = int(now_ms)

    def _expired(self, wid, now_ms):
        return self._status[wid] == INSPECTED_EMPTY and int(now_ms) - self._stamp_ms[wid] > self.inspected_ttl_ms

    def _age_s(self, wid, now_ms):
        stamp = self._stamp_ms[wid]
        return None if stamp is None else max(0, int(now_ms) - stamp) // 1000

    def _effective(self, wid, now_ms):
        return VISITED if self._expired(wid, now_ms) else self._status[wid]

    def _is_destination(self, wid, cid):
        dest = self.destination or {}
        if cid is not None and dest.get("command_id") is not None:
            return cid == dest["command_id"]  # a superseded goto must not clear its successor
        return bool(dest) and wid == dest["waypoint_id"]

    def _target(self, xy, now_ms):
        unvisited = [wid for wid, status in self._status.items() if status == UNVISITED]
        if unvisited:
            return self._nearest(xy, unvisited)
        expired = [wid for wid in self._status if self._expired(wid, now_ms)]
        return min(expired, key=lambda wid: self._stamp_ms[wid]) if expired else None
