import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_target_id import TargetIdentifier, colour_fraction, torso_region  # noqa: E402

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")


def frame_with_shirt(bgr, box=(100, 40, 180, 300)):
    img = np.full((360, 480, 3), (200, 200, 200), dtype=np.uint8)  # light grey room
    x1, y1, x2, y2 = box
    h = y2 - y1
    img[y1 + int(0.15 * h): y1 + int(0.55 * h), x1 + 16: x2 - 16] = bgr  # torso band
    return img


def track(box=(100, 40, 180, 300)):
    return {"track_id": 1, "box": list(box), "keypoints": [], "kp_conf": []}


def test_torso_region_falls_back_to_box_band_without_keypoints():
    assert torso_region(track(), 480, 360) == (116, 79, 164, 183)


def test_red_shirt_is_grandma_and_blue_is_not():
    ident = TargetIdentifier(name="Grandma", colour="red", min_fraction=0.35)
    red = frame_with_shirt((0, 0, 220))
    t = track()
    assert ident.apply(red, [t]) == [1]
    assert t["identity"] == {"name": "Grandma", "score": pytest.approx(1.0, abs=0.05), "method": "shirt_colour"}
    blue = frame_with_shirt((220, 0, 0))
    t2 = track()
    assert ident.apply(blue, [t2]) == []
    assert t2.get("identity") is None and t2["shirt_colour_fraction"] < 0.05


def test_colour_fraction_ignores_dark_and_washed_out_pixels():
    img = frame_with_shirt((40, 40, 55))  # dark, barely saturated: not a red shirt
    assert colour_fraction(img, torso_region(track(), 480, 360), "red") < 0.05


def test_unknown_colour_is_rejected():
    with pytest.raises(ValueError):
        TargetIdentifier(colour="tartan")
