"""Coarse posture from 2-D pose keypoints.

Pure math on one person's COCO-17 keypoints and bounding box, all in image
pixel coordinates (x right, y down). The output is a coarse image-space
estimate for the demo fall trigger: it is not a diagnosis, not a calibrated
measurement, and says nothing about 3-D body orientation. When the evidence
is weak or the two cues disagree the answer is ``unknown`` rather than a guess.

COCO-17 order: 0 nose, 1-2 eyes, 3-4 ears, 5-6 shoulders, 7-8 elbows,
9-10 wrists, 11-12 hips, 13-14 knees, 15-16 ankles.
"""

from __future__ import annotations

import math

UPRIGHT, LYING, UNKNOWN = "upright", "lying", "unknown"

SHOULDER_IDS = (5, 6)
HIP_IDS = (11, 12)
MIN_TORSO_POINTS = 3  # of the 4 shoulder/hip keypoints

LYING_MIN_ANGLE_DEG = 60.0
UPRIGHT_MAX_ANGLE_DEG = 30.0
LYING_BOX_ASPECT = 1.1  # width must exceed height * this
UPRIGHT_BOX_ASPECT = 0.9  # height must be at least width * this


def _unknown(reason: str, angle: float | None = None) -> dict:
    return {"posture": UNKNOWN, "torso_angle_deg": angle, "reason": reason}


def _mean_point(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def classify_posture(
    keypoints_xy: list[tuple[float, float]],
    keypoint_conf: list[float],
    box_xyxy: tuple[float, float, float, float],
    *,
    min_conf: float = 0.3,
) -> dict:
    """Classify one person as upright, lying, or unknown.

    The torso vector runs from the mean confident shoulder to the mean
    confident hip. ``torso_angle_deg`` is its angle from the downward image
    vertical in [0, 180]: 0 is head-up, 90 is horizontal, above 90 means the
    hips are above the shoulders in the image. Lying needs a near-horizontal
    (or inverted) torso and a wide box; upright needs a near-vertical torso
    and a tall box; anything else is unknown.
    """
    needed = max(HIP_IDS) + 1
    if len(keypoints_xy) < needed or len(keypoint_conf) < needed:
        return _unknown("expected COCO-17 keypoints with confidences")

    def confident(ids: tuple[int, ...]) -> list[tuple[float, float]]:
        points = []
        for i in ids:
            x, y = float(keypoints_xy[i][0]), float(keypoints_xy[i][1])
            conf = float(keypoint_conf[i])
            if math.isfinite(x) and math.isfinite(y) and conf >= min_conf:
                points.append((x, y))
        return points

    shoulders = confident(SHOULDER_IDS)
    hips = confident(HIP_IDS)
    found = len(shoulders) + len(hips)
    if found < MIN_TORSO_POINTS:
        return _unknown(
            f"only {found} of 4 shoulder/hip keypoints at conf >= {min_conf}"
        )

    x_min, y_min, x_max, y_max = (float(v) for v in box_xyxy)
    width, height = x_max - x_min, y_max - y_min
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        return _unknown("degenerate bounding box")

    shoulder_x, shoulder_y = _mean_point(shoulders)
    hip_x, hip_y = _mean_point(hips)
    dx, dy = hip_x - shoulder_x, hip_y - shoulder_y
    if math.hypot(dx, dy) < 1e-6:
        return _unknown("shoulders and hips coincide")

    angle = round(math.degrees(math.atan2(abs(dx), dy)), 1)
    if angle >= LYING_MIN_ANGLE_DEG and width > height * LYING_BOX_ASPECT:
        return {
            "posture": LYING,
            "torso_angle_deg": angle,
            "reason": f"torso {angle} deg from vertical in a wide box",
        }
    if angle <= UPRIGHT_MAX_ANGLE_DEG and height >= width * UPRIGHT_BOX_ASPECT:
        return {
            "posture": UPRIGHT,
            "torso_angle_deg": angle,
            "reason": f"torso {angle} deg from vertical in a tall box",
        }
    return _unknown(
        f"torso {angle} deg from vertical with a {width:.0f}x{height:.0f} box is ambiguous",
        angle,
    )
