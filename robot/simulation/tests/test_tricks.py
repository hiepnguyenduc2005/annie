"""Pure tests for trick velocity scripts and the navigator's tricking state (no MuJoCo)."""
import math

import pytest

from robot.simulation.tricks import TRICKS, LIMITS, trick_velocity, trick_duration_s


def test_every_trick_is_a_bounded_velocity_script():
    assert set(TRICKS) == {"spin", "circle", "zigzag", "wiggle", "figure8"}
    for name, segments in TRICKS.items():
        assert segments, name
        for vx, vy, wz, seconds in segments:
            assert abs(vx) <= LIMITS[0] and abs(vy) <= LIMITS[1] and abs(wz) <= LIMITS[2], name
            assert 0 < seconds <= 10, name
        assert 1.0 <= trick_duration_s(name) <= 30.0, name


def test_spin_integrates_to_one_full_turn():
    yaw = sum(wz * s for _, _, wz, s in TRICKS["spin"])
    assert yaw == pytest.approx(2 * math.pi, rel=0.05)


def test_circle_returns_near_start_in_the_body_frame_model():
    # Constant vx and wz trace a circle of radius vx/wz; one full turn returns home.
    (vx, vy, wz, s), = TRICKS["circle"]
    assert vy == 0 and wz * s == pytest.approx(2 * math.pi, rel=0.05)
    assert 0.5 <= vx / wz <= 1.5


def test_velocity_lookup_by_elapsed_time_and_end():
    assert trick_velocity("wiggle", 0.0) == TRICKS["wiggle"][0][:3]
    total = trick_duration_s("wiggle")
    assert trick_velocity("wiggle", total - 0.001) == TRICKS["wiggle"][-1][:3]
    assert trick_velocity("wiggle", total) is None
    assert trick_velocity("wiggle", -1.0) == TRICKS["wiggle"][0][:3]


def test_unknown_trick_rejected():
    with pytest.raises(ValueError):
        trick_duration_s("backflip")


def test_trick_footprint_is_the_kinematic_path_of_the_script():
    from robot.simulation.tricks import trick_footprint
    # A spin in place stays put; a circle of radius 0.5 m reaches 1.0 m from the start.
    spin = trick_footprint("spin", (0.0, 0.0), 0.0)
    assert max(abs(x) + abs(y) for x, y in spin) < 0.05
    circle = trick_footprint("circle", (0.0, 0.0), 0.0)
    far = max(math.hypot(x, y) for x, y in circle)
    assert 0.8 <= far <= 1.2
    # Heading matters: the same zigzag facing +x versus -x lands on opposite sides.
    east = trick_footprint("zigzag", (0.0, 0.0), 0.0)[-1]
    west = trick_footprint("zigzag", (0.0, 0.0), math.pi)[-1]
    assert east[0] > 0.5 and west[0] < -0.5
