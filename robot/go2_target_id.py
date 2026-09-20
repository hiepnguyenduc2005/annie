"""Demo-day target identification by clothing colour: "the person in the red shirt is Grandma".

Face matching (`robot/simulation/face_id.py`) needs enrolled photos and a clear frontal
face; from a dog's-eye camera in a crowd that is unreliable, so the demo uses a notable
garment instead. `TargetIdentifier` measures the fraction of a track's torso pixels that
fall in a named hue band (HSV) and, above a threshold, attaches
`identity = {"name": <target>, "score": <fraction>, "method": "shirt_colour"}` to the
track. It is a colour match, not an identification: anyone in a red shirt is "Grandma"
for the demo, and a red bag or poster on the torso line will match too. The torso is
taken between the shoulder and hip keypoints when they are confident, else the middle
band of the box, so legs-only or back views still get a reasonable sample.
"""
from __future__ import annotations

# OpenCV hue is 0..179. Red wraps around zero.
HUE_BANDS = {
    "red": ((0, 10), (170, 179)),
    "orange": ((11, 22),),
    "yellow": ((23, 35),),
    "green": ((36, 85),),
    "blue": ((90, 130),),
    "purple": ((131, 160),),
    "pink": ((161, 175),),
}


def torso_region(track, frame_w, frame_h, kp_conf_min=0.3):
    """(x1, y1, x2, y2) of the torso in pixels: shoulders->hips when known, else the box's middle band."""
    x1, y1, x2, y2 = (float(v) for v in track["box"])
    kps, kpc = track.get("keypoints") or [], track.get("kp_conf") or []

    def ok(i):
        return i < len(kps) and i < len(kpc) and kpc[i] >= kp_conf_min
    if all(ok(i) for i in (5, 6, 11, 12)):
        xs = [kps[i][0] for i in (5, 6, 11, 12)]
        ys = [kps[i][1] for i in (5, 6, 11, 12)]
        tx1, tx2 = min(xs), max(xs)
        ty1, ty2 = min(ys), max(ys)
        pad = 0.15 * max(1.0, tx2 - tx1)
        tx1, tx2 = tx1 - pad, tx2 + pad
    else:
        h = y2 - y1
        w = x2 - x1
        tx1, tx2 = x1 + 0.2 * w, x2 - 0.2 * w
        ty1, ty2 = y1 + 0.15 * h, y1 + 0.55 * h
    tx1, ty1 = max(0.0, tx1), max(0.0, ty1)
    tx2, ty2 = min(float(frame_w), tx2), min(float(frame_h), ty2)
    if tx2 - tx1 < 4 or ty2 - ty1 < 4:
        return None
    return int(tx1), int(ty1), int(tx2), int(ty2)


def colour_fraction(img_bgr, region, colour="red", s_min=90, v_min=60) -> float:
    """Fraction of region pixels whose hue is in the named band with enough saturation/brightness."""
    import cv2
    import numpy as np
    x1, y1, x2, y2 = region
    crop = img_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = np.zeros(h.shape, dtype=bool)
    for lo, hi in HUE_BANDS[colour]:
        mask |= (h >= lo) & (h <= hi)
    mask &= (s >= s_min) & (v >= v_min)
    return float(mask.mean())


class TargetIdentifier:
    """Attach a named identity to tracks whose torso is mostly the target colour."""

    def __init__(self, name="Grandma", colour="red", min_fraction=0.35):
        if colour not in HUE_BANDS:
            raise ValueError(f"unknown colour {colour!r}; choose from {sorted(HUE_BANDS)}")
        self.name, self.colour, self.min_fraction = name, colour, min_fraction
        self.matches = 0

    def apply(self, img_bgr, tracks):
        """Mutates tracks in place (sets/clears `identity` for this method); returns the matched track ids."""
        h, w = img_bgr.shape[:2]
        matched = []
        for t in tracks:
            region = torso_region(t, w, h)
            frac = colour_fraction(img_bgr, region, self.colour) if region else 0.0
            t["shirt_colour_fraction"] = round(frac, 3)
            existing = t.get("identity")
            if frac >= self.min_fraction:
                t["identity"] = {"name": self.name, "score": round(frac, 3), "method": "shirt_colour"}
                matched.append(t.get("track_id"))
                self.matches += 1
            elif existing and existing.get("method") == "shirt_colour":
                t["identity"] = None
        return matched
