"""Velocity choreography the trained locomotion policy can actually walk.

A trick is a list of (vx m/s, vy m/s, wz rad/s, seconds) segments played through
the same body-velocity interface as waypoint navigation, so displacement still
comes from physics and the person-safety interlock still applies. Nothing here
poses joints or teleports the base; there is no sit, flip or dance animation,
because the Go1 surrogate policy only takes body velocity. All values stay
inside the controller limits (0.6, 0.3, 1.0).
"""
from __future__ import annotations

import math

LIMITS = (0.6, 0.3, 1.0)

TRICKS: dict[str, list[tuple[float, float, float, float]]] = {
    # One full turn in place at 0.9 rad/s.
    "spin": [(0.0, 0.0, 0.9, 2 * math.pi / 0.9)],
    # Radius vx/wz = 0.4/0.8 = 0.5 m, one lap in 7.9 s.
    "circle": [(0.4, 0.0, 0.8, 2 * math.pi / 0.8)],
    # Three sidestep beats with a short forward push between them.
    "zigzag": [(0.25, 0.25, 0.0, 1.2), (0.25, -0.25, 0.0, 1.2), (0.25, 0.25, 0.0, 1.2),
               (0.25, -0.25, 0.0, 1.2), (0.0, 0.0, 0.0, 0.6)],
    # Fast alternating strafe: the "happy dog" move.
    "wiggle": [(0.0, 0.3, 0.0, 0.5), (0.0, -0.3, 0.0, 0.5), (0.0, 0.3, 0.0, 0.5),
               (0.0, -0.3, 0.0, 0.5), (0.0, 0.3, 0.0, 0.5), (0.0, -0.3, 0.0, 0.5), (0.0, 0.0, 0.0, 0.5)],
    # Two lobes of radius 0.4/0.8 = 0.5 m turned opposite ways (7.9 s each).
    "figure8": [(0.4, 0.0, 0.8, 2 * math.pi / 0.8), (0.4, 0.0, -0.8, 2 * math.pi / 0.8)],
}


def _segments(name: str):
    try:
        return TRICKS[name]
    except KeyError:
        raise ValueError(f"unknown trick: {name}") from None


def trick_duration_s(name: str) -> float:
    return float(sum(seg[3] for seg in _segments(name)))


def trick_velocity(name: str, elapsed_s: float):
    """Body velocity (vx, vy, wz) at elapsed seconds, or None once the script ends."""
    t = 0.0
    segments = _segments(name)
    if elapsed_s < 0:
        return segments[0][:3]
    for vx, vy, wz, seconds in segments:
        if elapsed_s < t + seconds:
            return (vx, vy, wz)
        t += seconds
    return None


def trick_footprint(name: str, start_xy: tuple[float, float], yaw: float, step_s: float = 0.1):
    """Kinematic world-frame path of the script from a start pose (no physics).

    Integrates the commanded body velocities as if the policy tracked them
    exactly. Used as a pre-flight clearance check: it is an estimate of where
    the robot will be, not a measurement, so callers keep the live obstacle
    guard active during the trick.
    """
    x, y = float(start_xy[0]), float(start_xy[1])
    heading = float(yaw)
    points = [(x, y)]
    for vx, vy, wz, seconds in _segments(name):
        steps = max(1, int(round(seconds / step_s)))
        dt = seconds / steps
        for _ in range(steps):
            heading += wz * dt
            x += (vx * math.cos(heading) - vy * math.sin(heading)) * dt
            y += (vx * math.sin(heading) + vy * math.cos(heading)) * dt
            points.append((x, y))
    return points
