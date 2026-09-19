from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from robot.patrol import (
    Boundary, Limits, MotionProposal, PatrolSupervisor, Pose, Receipt,
    Telemetry, Verification, missing_verifications,
)


def sample(now=100.0, **changes):
    base = Telemetry("hardware", "boot-1", "odom-1", now, now, now, now,
                     Pose(0.0, 0.0, 0.02), 0.0, 50.0, True, True, True)
    return replace(base, **changes)


def verified(**changes):
    return replace(Verification(True, True, True, True, True, 0.2), **changes)


def proposal(now=100.0, **changes):
    return replace(MotionProposal(str(uuid4()), str(uuid4()), "boot-1", "odom-1", 1, now, now,
                                  0.1, 0.0, 0.1), **changes)


def supervisor(armed=True):
    gate = PatrolSupervisor(Boundary("odom-1", "boot-1", 0.0, 0.0, 2.5))
    if armed:
        assert gate.arm(sample(), verified(), now_s=100, goal_revision=1).reason == "armed_waiting_for_plan"
    return gate


def test_missing_verification_is_not_satisfied_by_connection_or_stationary_stop_ack():
    gate = supervisor(armed=False)
    assert gate.admit(proposal(), sample(), Verification(), now_s=100).disposition == "hold"
    assert gate.arm(sample(), Verification(operator_stop_verified=True), now_s=100,
                    goal_revision=1).reason == "moving_stop_verified_missing"
    assert not gate.armed
    assert len(missing_verifications(Verification())) == 6
    assert missing_verifications(verified()) == ()
    assert len(missing_verifications(None)) == 6
    assert "network or host-power loss" in missing_verifications(Verification())[2]


@pytest.mark.parametrize("flag", [
    "operator_stop_verified", "moving_stop_verified", "loss_link_stop_verified",
    "boundary_frame_verified", "obstacle_avoidance_verified",
])
def test_each_physical_verification_is_required_and_revocation_latches(flag):
    gate = supervisor()
    result = gate.check(sample(), verified(**{flag: False}), now_s=100)
    assert result.disposition == "latched_stop"
    assert result.reason == flag + "_missing"
    assert gate.check(sample(), verified(), now_s=100).disposition == "latched_stop"
    assert gate.arm(sample(), verified(), now_s=100, goal_revision=2).reason == "armed_waiting_for_plan"


@pytest.mark.parametrize("changes,reason", [
    ({"camera_at_s": 98.9}, "stale_or_invalid_camera_at_s"),
    ({"pose_at_s": 99.4}, "stale_or_invalid_pose_at_s"),
    ({"lidar_at_s": 99.4}, "stale_or_invalid_lidar_at_s"),
    ({"received_at_s": 99.4}, "stale_or_invalid_received_at_s"),
    ({"camera_at_s": 101}, "stale_or_invalid_camera_at_s"),
    ({"pose_at_s": float("nan")}, "stale_or_invalid_pose_at_s"),
    ({"connected": False}, "disconnected"),
    ({"source": "simulation"}, "hardware_source_required"),
    ({"map_id": "different"}, "coordinate_frame_changed"),
    ({"session_id": "reconnected"}, "coordinate_frame_changed"),
    ({"battery_pct": 20}, "battery_unavailable_or_low"),
    ({"battery_pct": None}, "battery_unavailable_or_low"),
    ({"speed_mps": 0.2}, "speed_limit_exceeded"),
    ({"pose": Pose(0, 0, 0.2)}, "pose_uncertainty_invalid"),
    ({"pose": Pose(float("inf"), 0, 0.02)}, "pose_uncertainty_invalid"),
    ({"obstacle_avoidance_enabled": False}, "obstacle_state_not_clear"),
    ({"obstacles_clear": None}, "obstacle_state_not_clear"),
])
def test_faults_latch_and_fresh_data_does_not_resume(changes, reason):
    gate = supervisor()
    result = gate.check(sample(**changes), verified(), now_s=100)
    assert (result.disposition, result.reason) == ("latched_stop", reason)
    assert gate.admit(proposal(), sample(), verified(), now_s=100).disposition == "latched_stop"


def test_boundary_is_fixed_and_reserves_body_uncertainty_and_measured_stop_distance():
    gate = supervisor()
    with pytest.raises(FrozenInstanceError):
        gate.boundary.radius_m = 5
    with pytest.raises(AttributeError):
        gate.boundary = Boundary("x", "y", 0, 0, 100)
    result = gate.admit(proposal(target_x_m=2), sample(), verified(), now_s=100)
    assert result.reason == "target_outside_stopping_boundary"
    result = gate.check(sample(pose=Pose(2.0, 0, 0.02)), verified(), now_s=100)
    assert result.reason == "boundary_stopping_margin_exhausted"


def test_pose_jump_and_backward_clock_require_explicit_recovery():
    gate = supervisor()
    assert gate.check(sample(100.1, pose=Pose(0.8, 0, 0.02)), verified(), now_s=100.1).reason == "pose_discontinuity"
    gate = supervisor()
    assert gate.check(sample(99.9), verified(), now_s=99.9).reason == "monotonic_clock_invalid"


def test_slow_model_original_frame_cannot_be_refreshed_by_a_current_camera():
    gate = supervisor()
    old = proposal()
    current = sample(106)
    result = gate.admit(replace(old, planned_at_s=106), current, verified(), now_s=106)
    assert result.reason == "plan_frame_expired_or_invalid"
    assert gate.pending_command_id is None
    assert gate.admit(proposal(106), current, verified(), now_s=106).disposition == "allow"


@pytest.mark.parametrize("changes,reason", [
    ({"session_id": "old-boot"}, "proposal_context_changed"),
    ({"map_id": "old-map"}, "proposal_context_changed"),
    ({"frame_id": "not-a-frame"}, "command_or_frame_id_invalid"),
    ({"command_id": "not-a-command"}, "command_or_frame_id_invalid"),
    ({"goal_revision": True}, "proposal_context_changed"),
    ({"speed_mps": float("nan")}, "proposal_motion_invalid"),
    ({"planned_at_s": 101}, "plan_frame_expired_or_invalid"),
])
def test_proposal_keeps_validated_frame_and_session_identity(changes, reason):
    gate = supervisor()
    assert gate.admit(proposal(**changes), sample(), verified(), now_s=100).reason == reason
    assert gate.pending_command_id is None


def test_pause_or_goal_change_during_model_call_requires_new_arm_and_frame():
    gate = supervisor()
    assert gate.check(sample(), verified(), now_s=100, enabled=False).disposition == "latched_stop"
    assert gate.arm(sample(101), verified(), now_s=101, goal_revision=1).reason == "armed_waiting_for_plan"
    assert gate.admit(proposal(), sample(101), verified(), now_s=101).reason == "plan_frame_expired_or_invalid"
    assert gate.check(sample(101), verified(), now_s=101, goal_revision=2).reason == "goal_changed"


def test_command_reserved_before_transport_and_uncertain_delivery_is_never_replayed():
    gate = supervisor()
    first = proposal()
    assert gate.admit(first, sample(), verified(), now_s=100).disposition == "allow"
    for next_proposal in (first, proposal()):
        result = gate.admit(next_proposal, sample(), verified(), now_s=100)
        assert (result.disposition, result.reason) == ("hold", "awaiting_terminal_receipt")
    assert gate.arm(sample(), verified(), now_s=100, goal_revision=2).disposition == "latched_stop"


def test_receipts_need_matching_identity_and_post_completion_measured_arrival():
    gate = supervisor()
    plan = proposal()
    gate.admit(plan, sample(), verified(), now_s=100)
    def receipt(status, ts=100):
        return Receipt(plan.command_id, status, ts)
    assert gate.record_receipt(Receipt(str(uuid4()), "completed", 100), sample(), verified(), now_s=100).reason == "unmatched_receipt_ignored"
    assert gate.record_receipt(receipt("executing"), sample(), verified(), now_s=100).reason == "awaiting_terminal_receipt"
    assert gate.record_receipt(receipt("accepted"), sample(), verified(), now_s=100).reason == "out_of_order_receipt_ignored"
    assert gate.record_receipt(receipt("completed", 100.2), sample(100.2, pose_at_s=100), verified(), now_s=100.2).reason == "awaiting_post_completion_pose"
    assert gate.record_receipt(receipt("completed", 100.2), sample(100.2), verified(), now_s=100.2).reason == "verified_arrival"
    assert gate.pending_command_id is None
    assert gate.admit(plan, sample(100.2), verified(), now_s=100.2).reason == "command_already_admitted"
    assert gate.admit(proposal(100.2, frame_id=plan.frame_id), sample(100.2), verified(), now_s=100.2).reason == "frame_already_used_for_motion"
    assert gate.admit(proposal(100.2), sample(100.2), verified(), now_s=100.2).disposition == "allow"


def test_false_completion_and_receipt_timeout_latch_without_retries():
    gate = supervisor()
    plan = proposal(target_x_m=1)
    gate.admit(plan, sample(), verified(), now_s=100)
    result = gate.record_receipt(Receipt(plan.command_id, "completed", 100), sample(), verified(), now_s=100)
    assert result.reason == "completion_without_measured_arrival"
    gate = supervisor()
    gate.admit(plan, sample(), verified(), now_s=100)
    assert gate.check(sample(161), verified(), now_s=161).reason == "command_receipt_timeout"


def test_failed_command_latches_and_requires_stopped_explicit_rearm():
    gate = supervisor()
    plan = proposal()
    gate.admit(plan, sample(), verified(), now_s=100)
    assert gate.record_receipt(Receipt(plan.command_id, "failed", 100), sample(), verified(), now_s=100).reason == "command_failed"
    assert gate.arm(sample(speed_mps=0.1), verified(), now_s=100, goal_revision=2).disposition == "latched_stop"
    assert gate.arm(sample(), verified(), now_s=100, goal_revision=2).reason == "armed_waiting_for_plan"


@pytest.mark.parametrize("factory", [
    lambda: Limits(plan_age_s=float("inf")),
    lambda: Limits(max_speed_mps=True),
    lambda: Limits(minimum_battery_pct=101),
    lambda: Limits(plan_age_s=6),
    lambda: Limits(supervision_interval_s=2),
    lambda: Boundary("m", "s", 0, 0, float("nan")),
])
def test_invalid_configuration_is_rejected(factory):
    with pytest.raises(ValueError):
        factory()
