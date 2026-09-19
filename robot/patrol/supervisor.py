"""Fail-closed physical patrol admission using caller-supplied evidence.

No I/O, model calls, transport retries, coordinate transforms or simulator
fallback. A latched_stop decision requests the adapter's stop procedure; it
does not prove that the hardware stopped. Independent onboard loss-link
protection is a prerequisite because host software cannot stop a lost link.
All timestamps are host monotonic seconds from one process/session.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import Literal
from uuid import UUID


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Pose:
    x_m: float
    y_m: float
    uncertainty_m: float


@dataclass(frozen=True)
class Boundary:
    map_id: str
    session_id: str
    origin_x_m: float
    origin_y_m: float
    radius_m: float

    def __post_init__(self):
        if (not isinstance(self.map_id, str) or not self.map_id or
                not isinstance(self.session_id, str) or not self.session_id or
                not all(_finite(v) for v in (self.origin_x_m, self.origin_y_m, self.radius_m)) or
                self.radius_m <= 0):
            raise ValueError("Boundary requires a named frame/session and finite positive radius")


@dataclass(frozen=True)
class Limits:
    """Conservative demo settings, not measured hardware guarantees."""

    telemetry_age_s: float = 0.5
    camera_age_s: float = 1.0
    lidar_age_s: float = 0.5
    plan_age_s: float = 5.0
    max_uncertainty_m: float = 0.15
    max_speed_mps: float = 0.15
    minimum_battery_pct: float = 25.0
    body_clearance_m: float = 0.4
    supervision_interval_s: float = 0.1
    arrival_tolerance_m: float = 0.3
    stopped_speed_mps: float = 0.05
    command_timeout_s: float = 60.0
    pose_jump_slack_m: float = 0.05

    def __post_init__(self):
        if any(not _finite(getattr(self, f.name)) or getattr(self, f.name) <= 0
               for f in fields(self)):
            raise ValueError("Every patrol limit must be finite and positive")
        if self.minimum_battery_pct > 100 or self.stopped_speed_mps > self.max_speed_mps:
            raise ValueError("Battery or stopped-speed limit is inconsistent")
        if self.plan_age_s > 5 or self.supervision_interval_s > min(self.telemetry_age_s, self.camera_age_s, self.lidar_age_s):
            raise ValueError("Limits cannot weaken the five-second plan TTL or outlive sensor freshness")


@dataclass(frozen=True)
class Telemetry:
    source: Literal["hardware"]
    session_id: str
    map_id: str
    received_at_s: float
    pose_at_s: float
    camera_at_s: float
    lidar_at_s: float
    pose: Pose
    speed_mps: float
    battery_pct: float
    connected: bool
    obstacle_avoidance_enabled: bool
    obstacles_clear: bool


@dataclass(frozen=True)
class Verification:
    """Adapter/operator attestations from controlled physical tests.

    None/False means unverified. A stationary command ACK satisfies none of
    the stop flags. stop_distance_m bounds travel in the verified moving-stop
    AND loss-link tests at Limits.max_speed_mps, after detection of the fault.
    """

    operator_stop_verified: bool = False
    moving_stop_verified: bool = False
    loss_link_stop_verified: bool = False
    boundary_frame_verified: bool = False
    obstacle_avoidance_verified: bool = False
    stop_distance_m: float | None = None


def missing_verifications(verification: Verification | None) -> tuple[str, ...]:
    """Operator-facing unmet prerequisites; a connection/ACK proves none."""
    requirements = (
        ("operator_stop_verified", "Verify the operator's physical stop and recovery method."),
        ("moving_stop_verified", "Measure stopping from motion at the configured maximum speed."),
        ("loss_link_stop_verified", "Verify onboard stopping after network or host-power loss."),
        ("boundary_frame_verified", "Align and verify the physical map, origin and patrol boundary."),
        ("obstacle_avoidance_verified", "Verify obstacle avoidance behavior in the controlled area."),
    )
    gaps = [message for name, message in requirements
            if not isinstance(verification, Verification) or getattr(verification, name) is not True]
    if (not isinstance(verification, Verification) or not _finite(verification.stop_distance_m) or
            verification.stop_distance_m < 0):
        gaps.append("Record a measured stopping-distance bound covering moving-stop and loss-link tests.")
    return tuple(gaps)


@dataclass(frozen=True)
class MotionProposal:
    command_id: str
    frame_id: str
    session_id: str
    map_id: str
    goal_revision: int
    frame_at_s: float
    planned_at_s: float
    target_x_m: float
    target_y_m: float
    speed_mps: float


@dataclass(frozen=True)
class Receipt:
    command_id: str
    status: Literal["accepted", "executing", "completed", "failed"]
    received_at_s: float


@dataclass(frozen=True)
class Decision:
    disposition: Literal["allow", "hold", "latched_stop"]
    reason: str
    command_id: str | None = None


class PatrolSupervisor:
    """Single owner gate. Call check regularly even while a model is blocked.

    admit reserves a command before transport starts. An uncertain send stays
    reserved; retrying the same ID is never authorized here. arm is an explicit
    operator action, requires a stopped robot and cannot erase pending motion.
    Reconnect/relocalization needs a NEW supervisor with a verified boundary.
    """

    def __init__(self, boundary: Boundary, limits: Limits = Limits()):
        self._boundary, self._limits = boundary, limits
        self._armed = False
        self._latched = None
        self._pending = None
        self._pending_at_s = None
        self._seen_commands = set()
        self._seen_frames = set()
        self._goal_revision = None
        self._armed_at_s = None
        self._last_now = None
        self._last_pose = None
        self._last_pose_at = None
        self._receipt_rank = 0

    @property
    def boundary(self):
        return self._boundary

    @property
    def limits(self):
        return self._limits

    @property
    def armed(self):
        return self._armed and self._latched is None

    @property
    def pending_command_id(self):
        return self._pending.command_id if self._pending else None

    def _fault(self, reason):
        if self._armed or self._pending or self._latched:
            self._latched = self._latched or reason
            self._armed = False
            return Decision("latched_stop", self._latched, self.pending_command_id)
        return Decision("hold", reason)

    def _margin(self, sample, verification):
        return (self.limits.body_clearance_m + sample.pose.uncertainty_m +
                verification.stop_distance_m +
                self.limits.max_speed_mps * self.limits.supervision_interval_s)

    def _inside(self, x, y, margin):
        return math.hypot(x - self.boundary.origin_x_m, y - self.boundary.origin_y_m) + margin < self.boundary.radius_m

    def _validate(self, sample, verification, now_s):
        if not _finite(now_s) or now_s < 0 or (self._last_now is not None and now_s < self._last_now):
            return "monotonic_clock_invalid"
        self._last_now = now_s
        if not isinstance(sample, Telemetry) or not isinstance(sample.pose, Pose):
            return "telemetry_missing"
        if sample.source != "hardware":
            return "hardware_source_required"
        if sample.session_id != self.boundary.session_id or sample.map_id != self.boundary.map_id:
            return "coordinate_frame_changed"
        if sample.connected is not True:
            return "disconnected"
        for name, age in (("received_at_s", self.limits.telemetry_age_s),
                          ("pose_at_s", self.limits.telemetry_age_s),
                          ("camera_at_s", self.limits.camera_age_s),
                          ("lidar_at_s", self.limits.lidar_age_s)):
            ts = getattr(sample, name)
            if not _finite(ts) or not 0 <= ts <= sample.received_at_s <= now_s or now_s - ts > age:
                return "stale_or_invalid_" + name
        p = sample.pose
        if not all(_finite(v) for v in (p.x_m, p.y_m, p.uncertainty_m)) or not 0 <= p.uncertainty_m <= self.limits.max_uncertainty_m:
            return "pose_uncertainty_invalid"
        if not _finite(sample.speed_mps) or not 0 <= sample.speed_mps <= self.limits.max_speed_mps:
            return "speed_limit_exceeded"
        if not _finite(sample.battery_pct) or not self.limits.minimum_battery_pct <= sample.battery_pct <= 100:
            return "battery_unavailable_or_low"
        if sample.obstacle_avoidance_enabled is not True or sample.obstacles_clear is not True:
            return "obstacle_state_not_clear"
        if not isinstance(verification, Verification):
            return "verification_missing"
        for name in ("operator_stop_verified", "moving_stop_verified", "loss_link_stop_verified",
                     "boundary_frame_verified", "obstacle_avoidance_verified"):
            if getattr(verification, name) is not True:
                return name + "_missing"
        if not _finite(verification.stop_distance_m) or verification.stop_distance_m < 0:
            return "measured_stop_distance_missing"
        if not self._inside(p.x_m, p.y_m, self._margin(sample, verification)):
            return "boundary_stopping_margin_exhausted"
        if self._last_pose_at is not None:
            dt = sample.pose_at_s - self._last_pose_at
            distance = math.hypot(p.x_m - self._last_pose.x_m, p.y_m - self._last_pose.y_m)
            tolerance = (p.uncertainty_m + self._last_pose.uncertainty_m +
                         self.limits.pose_jump_slack_m + self.limits.max_speed_mps * max(dt, 0))
            if dt < 0 or (dt == 0 and distance > 0) or distance > tolerance:
                return "pose_discontinuity"
        self._last_pose, self._last_pose_at = p, sample.pose_at_s
        return None

    def arm(self, sample, verification, *, now_s, goal_revision):
        if self._pending:
            return self._fault("unresolved_command_before_rearm")
        error = self._validate(sample, verification, now_s)
        if error:
            return self._fault(error)
        if sample.speed_mps > self.limits.stopped_speed_mps:
            return self._fault("robot_not_stopped")
        if type(goal_revision) is not int or goal_revision < 0:
            return self._fault("goal_revision_invalid")
        self._goal_revision = goal_revision
        self._armed_at_s = now_s
        self._armed, self._latched = True, None
        return Decision("hold", "armed_waiting_for_plan")

    def check(self, sample, verification, *, now_s, enabled=True, goal_revision=None):
        error = self._validate(sample, verification, now_s)
        if error:
            return self._fault(error)
        if enabled is not True:
            return self._fault("operator_paused")
        if goal_revision is not None and (type(goal_revision) is not int or goal_revision != self._goal_revision):
            return self._fault("goal_changed")
        if self._latched:
            return Decision("latched_stop", self._latched, self.pending_command_id)
        if not self._armed:
            return Decision("hold", "operator_arm_required")
        if self._pending:
            if now_s - self._pending_at_s > self.limits.command_timeout_s:
                return self._fault("command_receipt_timeout")
            return Decision("hold", "awaiting_terminal_receipt", self.pending_command_id)
        return Decision("hold", "armed_waiting_for_plan")

    def admit(self, proposal, sample, verification, *, now_s, enabled=True, goal_revision=None):
        decision = self.check(sample, verification, now_s=now_s, enabled=enabled, goal_revision=goal_revision)
        if decision.reason != "armed_waiting_for_plan":
            return decision
        if not isinstance(proposal, MotionProposal):
            return Decision("hold", "proposal_missing")
        try:
            valid_id = (str(UUID(proposal.command_id)) == proposal.command_id and
                        str(UUID(proposal.frame_id)) == proposal.frame_id)
        except (ValueError, TypeError, AttributeError):
            valid_id = False
        if not valid_id:
            return Decision("hold", "command_or_frame_id_invalid")
        if proposal.command_id in self._seen_commands:
            return Decision("hold", "command_already_admitted", proposal.command_id)
        if proposal.frame_id in self._seen_frames:
            return Decision("hold", "frame_already_used_for_motion")
        if len(self._seen_commands) >= 4096:
            return self._fault("session_command_limit_reached")
        if (proposal.session_id != self.boundary.session_id or proposal.map_id != self.boundary.map_id or type(proposal.goal_revision) is not int or
                proposal.goal_revision != self._goal_revision):
            return Decision("hold", "proposal_context_changed")
        if (not all(_finite(t) for t in (proposal.frame_at_s, proposal.planned_at_s)) or
                not self._armed_at_s <= proposal.frame_at_s <= proposal.planned_at_s <= now_s or
                now_s - proposal.frame_at_s > self.limits.plan_age_s):
            return Decision("hold", "plan_frame_expired_or_invalid")
        if (not all(_finite(v) for v in (proposal.target_x_m, proposal.target_y_m, proposal.speed_mps)) or
                not 0 < proposal.speed_mps <= self.limits.max_speed_mps):
            return Decision("hold", "proposal_motion_invalid")
        if not self._inside(proposal.target_x_m, proposal.target_y_m, self._margin(sample, verification)):
            return Decision("hold", "target_outside_stopping_boundary")
        self._pending, self._pending_at_s, self._receipt_rank = proposal, now_s, 0
        self._seen_commands.add(proposal.command_id)
        self._seen_frames.add(proposal.frame_id)
        return Decision("allow", "motion_reserved_once", proposal.command_id)

    def record_receipt(self, receipt, sample, verification, *, now_s):
        decision = self.check(sample, verification, now_s=now_s)
        if not self._pending:
            return decision
        if not isinstance(receipt, Receipt) or receipt.command_id != self.pending_command_id:
            return Decision(decision.disposition, "unmatched_receipt_ignored" if decision.disposition != "latched_stop" else decision.reason, self.pending_command_id)
        if (not _finite(receipt.received_at_s) or
                not self._pending_at_s <= receipt.received_at_s <= now_s):
            return self._fault("receipt_time_invalid")
        ranks = {"accepted": 1, "executing": 2, "completed": 3, "failed": 3}
        if not isinstance(receipt.status, str) or receipt.status not in ranks:
            return self._fault("receipt_status_invalid")
        if receipt.status == "failed":
            self._pending = None
            return self._fault("command_failed")
        if decision.disposition == "latched_stop":
            return decision
        if ranks[receipt.status] < self._receipt_rank:
            return Decision("hold", "out_of_order_receipt_ignored", self.pending_command_id)
        self._receipt_rank = ranks[receipt.status]
        if receipt.status != "completed":
            return Decision("hold", "awaiting_terminal_receipt", self.pending_command_id)
        if sample.pose_at_s < receipt.received_at_s:
            return Decision("hold", "awaiting_post_completion_pose", self.pending_command_id)
        distance = math.hypot(sample.pose.x_m - self._pending.target_x_m,
                              sample.pose.y_m - self._pending.target_y_m)
        if distance + sample.pose.uncertainty_m > self.limits.arrival_tolerance_m or sample.speed_mps > self.limits.stopped_speed_mps:
            return self._fault("completion_without_measured_arrival")
        command_id, self._pending = self.pending_command_id, None
        return Decision("hold", "verified_arrival", command_id)
