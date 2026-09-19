"""Bounded autonomous mission coordinator.

Deterministic, transparent policies over measured application status, the
ingested dog map, viewer state, and caller-supplied timestamps. This module
never calls a model or the network: every decision below is a reviewable
rule, not inferred reasoning.

Command contract (integrator-facing; see robot/simulation/AUTONOMY.md):

- decide() returns None or exactly one command dict:
    {"cmd": "goto" | "stop" | "look", "token": "auto-0001",
     "mode": <current mode>, "reason": <stable short reason>,
     "waypoint": <map waypoint id, goto only>}

- The integrator translates the command to the app command API, tracks the
  resulting app command, and reports receipts back through
  viewer_state["receipts"][token] with statuses "accepted", "executing",
  "completed", or "failed".
- goto progression always waits for the completed receipt AND a measured
  pose near the target waypoint; a failed or unconfirmed goto latches a
  hold until the operator explicitly resumes.
- Person latch release is advisory: the root must still satisfy
  PersonSafety.explicit_restart() semantics before issuing a new mission.
"""
from __future__ import annotations

import math

MIN_COMMAND_INTERVAL_MS = 2000
TELEMETRY_MAX_AGE_MS = 5000
POSE_TOLERANCE_M = 0.75
GROUND_LEVELS = (0, 0.0, "0", "ground", "main")
EVIDENCE_STOPS = ("stale-telemetry", "map-identity-mismatch", "invalid-pose")


class Autonomy:
    """Operator-select bounded autonomy: patrol, watch, paused, find_resident."""

    MODES = ("patrol", "watch", "paused", "find_resident")

    def __init__(self, *, capabilities=None):
        self.capabilities = dict(capabilities or {})
        self.mode = "paused"
        self.seq = 0
        self.last_issue_ms = None
        self.last_command = None
        self.resume_requested = False
        self.person_latch = False
        self.checkin_hold = False
        self.anomaly_hold = False
        self.stale_stop_issued = False
        self.last_observed = None
        self.search = None
        self.hold_reason = None
        self.patrol_index = 0
        self.patrol_started = False
        self.pause_stop_requested = False

    def set_mode(self, mode):
        """Operator-select the autonomy mode; anything else raises."""
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}")
        self.mode = mode
        if mode == "patrol":
            # Patrol entry or re-entry is the explicit resume gesture: it
            # clears the goto-failure latch, the retired command memory, and
            # restarts the route anchor.
            self.resume_requested = True
            self.anomaly_hold = False
            self.last_command = None
            self.patrol_started = False
            self.pause_stop_requested = False
        elif mode == "find_resident":
            # One search episode per explicit entry; never self-rearming. The
            # entry itself asserts fresh safe absence and clears the latch
            # only when viewer evidence confirms it.
            self.resume_requested = True
            self.search = {"phase": "goto", "look_used": False}
        else:
            # watch/paused explicitly stop existing motion on mode change.
            self.pause_stop_requested = True

    # ------------------------------------------------------------------ #
    # Decision entry point
    # ------------------------------------------------------------------ #
    def decide(self, status, map_data, viewer_state, now_ms):
        """Return at most one safe command for the current measured state."""
        now = int(now_ms)
        status = status if isinstance(status, dict) else {}
        map_data = map_data if isinstance(map_data, dict) else {}
        viewer_state = viewer_state if isinstance(viewer_state, dict) else {}
        dog = status.get("dog") if isinstance(status.get("dog"), dict) else {}
        nav = viewer_state.get("navigation") if isinstance(viewer_state.get("navigation"), dict) else {}
        safety = viewer_state.get("person_safety") if isinstance(viewer_state.get("person_safety"), dict) else {}
        receipts = viewer_state.get("receipts") if isinstance(viewer_state.get("receipts"), dict) else {}
        pose = dog.get("pose") or nav.get("pose")

        self._update_outcome(receipts)
        self._update_person(safety, pose, map_data, now)
        self._consume_resume(status, safety)

        holds = self._holds(status, dog, pose, map_data, now)
        if holds:
            self.hold_reason = "+".join(sorted(holds))
            return self._hold_command(holds, nav, now)

        # Fresh telemetry and no hold: motion may only start here.
        self.stale_stop_issued = False
        self.hold_reason = None
        if self.pause_stop_requested:
            self.pause_stop_requested = False
            if self._nav_active(nav) and self._rate_ok(now):
                return self._issue("stop", None, "mode-change-stop", now)
            return None
        if nav.get("state") == "moving":
            return None  # A mission is already executing; await its outcome.
        if self.mode == "find_resident":
            return self._decide_search(pose, map_data, nav, now)
        if self.mode == "patrol":
            return self._decide_patrol(pose, map_data, nav, now)
        return None  # watch / paused never initiate motion.

    # ------------------------------------------------------------------ #
    # Policy pieces
    # ------------------------------------------------------------------ #
    def _update_outcome(self, receipts):
        if not self.last_command:
            return
        reported = receipts.get(self.last_command["token"])
        if reported in ("accepted", "executing", "completed", "failed"):
            self.last_command["outcome"] = reported

    def _update_person(self, safety, pose, map_data, now):
        detections = safety.get("detections") or []
        # A blocked guard (stale frame, latched stop, or a sighting) holds
        # motion even when the latest frame carries no detections.
        if safety.get("blocked"):
            self.person_latch = True
            self.resume_requested = False  # A blocked view invalidates resume.
        if safety.get("ready") and detections:
            self.person_latch = True
            self.resume_requested = False  # A sighting invalidates any resume.
            self.last_observed = {
                "waypoint": self._nearest_waypoint_id(pose, map_data),
                "frame_id": safety.get("frame_id"),
                "ts_ms": safety.get("captured_at") if safety.get("captured_at") is not None else now,
            }

    def _consume_resume(self, status, safety):
        if not self.resume_requested:
            return
        pending = bool(status.get("pending_checkin"))
        safety_clear = bool(safety.get("ready")) and not safety.get("blocked")
        if self.person_latch and safety_clear and not pending:
            self.person_latch = False
            self.resume_requested = False
        elif not self.person_latch and not pending and not self.anomaly_hold:
            self.checkin_hold = False
            self.resume_requested = False

    def _holds(self, status, dog, pose, map_data, now):
        holds = []
        stale = self._stale_reason(dog, pose, map_data, now)
        if stale:
            holds.append(stale)
        if status.get("pending_checkin"):
            if not self.checkin_hold:
                self.checkin_hold = True
                self.resume_requested = False  # Risk invalidates any resume.
        if self.checkin_hold:
            holds.append("checkin-hold")
        if self.person_latch:
            holds.append("person-latch")
        if self.anomaly_hold:
            holds.append("goto-progress-anomaly")
        if dog.get("state") == "estop":
            holds.append("estop")
        return holds

    def _hold_command(self, holds, nav, now):
        if any(h in EVIDENCE_STOPS for h in holds):
            # No fresh evidence: stop once per episode, then silence.
            if not self.stale_stop_issued and self._rate_ok(now):
                self.stale_stop_issued = True
                return self._issue("stop", None, self.hold_reason, now)
            return None
        if self._nav_active(nav) and self._rate_ok(now):
            return self._issue("stop", None, self.hold_reason, now)
        return None

    @staticmethod
    def _nav_active(nav):
        """Motion includes driving, turning, and scanning, not just moving."""
        return nav.get("state") not in (None, "idle", "stopped", "failed")

    def _decide_patrol(self, pose, map_data, nav, now):
        waypoints = self._waypoints(map_data)
        if not waypoints:
            return None
        if not self.patrol_started:
            # Never re-target the waypoint the robot already occupies.
            self.patrol_index = (self._nearest_index(pose, waypoints) + 1) % len(waypoints)
            self.patrol_started = True
        last = self.last_command
        if last and last["cmd"] == "goto" and last["outcome"] in (None, "accepted", "executing"):
            return None  # Outstanding goto; await identified receipt.
        if last and last["cmd"] == "goto" and last["outcome"] == "failed":
            # Never retry a failed mission blindly.
            self.anomaly_hold = True
            self.hold_reason = "goto-progress-anomaly"
            return None
        if last and last["cmd"] == "goto" and last["outcome"] == "completed":
            if not self._pose_near(pose, map_data, last["waypoint"]):
                # Completed receipt without measured progress.
                self.anomaly_hold = True
                self.hold_reason = "goto-progress-anomaly"
                return None
            self._advance_patrol(map_data, last["waypoint"])
        if not self._rate_ok(now):
            return None
        target = waypoints[self.patrol_index % len(waypoints)]
        return self._issue("goto", target["id"], "periodic-patrol", now)

    def _decide_search(self, pose, map_data, nav, now):
        search = self.search
        if not search or search["phase"] == "done":
            return None
        observed = (self.last_observed or {}).get("waypoint")
        waypoints = self._waypoints(map_data)
        if not observed or observed not in {wp["id"] for wp in waypoints}:
            return None  # No known last-observed place; never wander.
        last = self.last_command
        if last and last["cmd"] == "goto" and last["outcome"] in (None, "accepted", "executing"):
            return None  # Any outstanding goto must resolve first.
        if search["phase"] == "goto":
            if last and last["cmd"] == "goto" and last["waypoint"] == observed:
                if last["outcome"] is None:
                    return None
                if last["outcome"] == "failed":
                    search["phase"] = "done"
                    return None
                if last["outcome"] == "completed":
                    if not self._pose_near(pose, map_data, observed):
                        self.anomaly_hold = True
                        self.hold_reason = "goto-progress-anomaly"
                        return None
                    search["phase"] = "look"
            elif self._rate_ok(now):
                return self._issue("goto", observed, "find-resident-search", now)
        if search["phase"] == "look" and not search["look_used"] and self._rate_ok(now):
            search["look_used"] = True
            command = self._issue("look", None, "find-resident-look", now)
            search["phase"] = "done"
            return command
        return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _issue(self, cmd, waypoint, reason, now):
        self.seq += 1
        token = "auto-%04d" % self.seq
        self.last_command = {"cmd": cmd, "waypoint": waypoint, "token": token,
                             "issued_at_ms": now, "outcome": None}
        self.last_issue_ms = now
        command = {"cmd": cmd, "token": token, "mode": self.mode, "reason": reason}
        if waypoint is not None:
            command["waypoint"] = waypoint
        return command

    def _rate_ok(self, now):
        return self.last_issue_ms is None or now - self.last_issue_ms >= MIN_COMMAND_INTERVAL_MS

    def _stale_reason(self, dog, pose, map_data, now):
        ts = dog.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not 0 <= now - ts <= TELEMETRY_MAX_AGE_MS:
            return "stale-telemetry"
        if not isinstance(pose, dict) or pose.get("map_id") != map_data.get("map_id"):
            return "map-identity-mismatch"
        if not self._finite(pose.get("x")) or not self._finite(pose.get("y")):
            return "invalid-pose"
        return None

    def _waypoints(self, map_data):
        usable = []
        for wp in map_data.get("waypoints") or []:
            if not isinstance(wp, dict):
                continue
            wid, x, y = wp.get("id"), wp.get("x"), wp.get("y")
            if not isinstance(wid, str) or not wid or not self._finite(x) or not self._finite(y):
                continue
            if self._off_ground(wp) and self.capabilities.get("upstairs") is not True:
                continue
            usable.append({"id": wid, "x": float(x), "y": float(y)})
        return usable

    @staticmethod
    def _off_ground(waypoint):
        """Floors metadata is upcoming; unknown levels stay off-limits."""
        for key in ("floor", "level"):
            if key in waypoint and waypoint[key] not in GROUND_LEVELS:
                return True
        if "z" in waypoint and waypoint["z"] not in (None, 0, 0.0):
            return True
        return False

    @staticmethod
    def _finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def _pose_near(self, pose, map_data, waypoint_id):
        if not isinstance(pose, dict):
            return False
        target = next((wp for wp in self._waypoints(map_data) if wp["id"] == waypoint_id), None)
        if target is None or not self._finite(pose.get("x")) or not self._finite(pose.get("y")):
            return False
        return math.hypot(pose["x"] - target["x"], pose["y"] - target["y"]) <= POSE_TOLERANCE_M

    def _nearest_waypoint_id(self, pose, map_data):
        waypoints = self._waypoints(map_data)
        if not waypoints or not isinstance(pose, dict) or not self._finite(pose.get("x")) or not self._finite(pose.get("y")):
            return None
        return waypoints[self._nearest_index(pose, waypoints)]["id"]

    @staticmethod
    def _nearest_index(pose, waypoints):
        if not isinstance(pose, dict) or not Autonomy._finite(pose.get("x")) or not Autonomy._finite(pose.get("y")):
            return 0
        return min(range(len(waypoints)),
                   key=lambda i: math.hypot(pose["x"] - waypoints[i]["x"], pose["y"] - waypoints[i]["y"]))

    def _advance_patrol(self, map_data, waypoint_id):
        waypoints = self._waypoints(map_data)
        for index, wp in enumerate(waypoints):
            if wp["id"] == waypoint_id:
                self.patrol_index = (index + 1) % max(len(waypoints), 1)
                return
