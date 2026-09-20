"""Render the Go2's forward camera through the furnished apartment and run the dog's person perception on it.

    PY=.cache/dimos/.venv/bin/python
    $PY robot/simulation/render_apartment.py --frames 30 --out output/simulation/apartment

Per variant it writes `front_NNN.jpg` (640x480 `robot_front` frames), `annotated_NNN.jpg`,
`contact_sheet.jpg`, `contact_sheet_annotated.jpg`, `overview.jpg` and `detections.json`.

What this is: rendered-image inference on direct MuJoCo renders. The dog is moved *kinematically*
along a scripted path (base pose and a cosmetic trot are written into qpos, then `mj_forward`);
no physics is stepped, no gait policy runs, and nothing here is DimOS or a physical robot run.
Perception mirrors the patrol process: `PersonTracker` with the `_fast_tracker` settings
(conf 0.30, imgsz 352, CPU), `plausible_people` with patrol's gates, then `TargetIdentifier`
("Jeanine" = red top). That identifier is a shirt-colour match, not an identification.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot.simulation import apartment  # noqa: E402

WIDTH, HEIGHT = 640, 480
FRAME_MS = 100  # the scripted walk is sampled as if the camera delivered 10 frames per second


def sample_path(waypoints, frames):
    """Chaikin-smoothed waypoints resampled at equal arc length -> [(x, y, yaw_rad)] * frames."""
    pts = [(x, y, math.radians(yaw)) for x, y, yaw in waypoints]
    for _ in range(3):
        smooth = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            smooth.append(tuple(0.75 * a[i] + 0.25 * b[i] for i in range(3)))
            smooth.append(tuple(0.25 * a[i] + 0.75 * b[i] for i in range(3)))
        smooth.append(pts[-1])
        pts = smooth
    lengths = [0.0]
    for a, b in zip(pts, pts[1:]):
        lengths.append(lengths[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    out, j = [], 0
    for k in range(frames):
        target = lengths[-1] * (k / max(1, frames - 1))
        while j < len(pts) - 2 and lengths[j + 1] < target:
            j += 1
        span = lengths[j + 1] - lengths[j]
        t = 0.0 if span <= 0 else min(1.0, (target - lengths[j]) / span)
        out.append(tuple(pts[j][i] + t * (pts[j + 1][i] - pts[j][i]) for i in range(3)) + (target,))
    return out


def pose_robot(mujoco, model, data, key_id, x, y, yaw, travelled):
    """Write a standing, mid-trot Go2 at (x, y, yaw). Cosmetic only: no dynamics are integrated."""
    mujoco.mj_resetDataKeyframe(model, data, key_id)
    phase = travelled / 0.22 * math.pi  # one diagonal pair swings per ~0.22 m
    roll, pitch = math.radians(0.7) * math.sin(phase), math.radians(0.5) * math.sin(2 * phase)
    cy, sy, cp, sp, cr, sr = (math.cos(yaw / 2), math.sin(yaw / 2), math.cos(pitch / 2), math.sin(pitch / 2),
                              math.cos(roll / 2), math.sin(roll / 2))
    data.qpos[0:3] = (x, y, 0.27 + 0.004 * math.sin(2 * phase))
    data.qpos[3:7] = (cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy)
    for leg, offset in enumerate((0.0, math.pi, math.pi, 0.0)):  # FL, FR, RL, RR: diagonal pairs in phase
        swing = math.sin(phase + offset)
        data.qpos[7 + 3 * leg + 1] = 0.9 + 0.22 * swing
        data.qpos[7 + 3 * leg + 2] = -1.8 - 0.28 * max(0.0, math.cos(phase + offset))
    mujoco.mj_forward(model, data)


def build_perception():
    """The patrol process's person pipeline: fast tracker settings, plausibility gates, red-top identifier."""
    from robot.dog.perception.pipeline import plausible_people
    from robot.dog.perception.target_id import TargetIdentifier
    from robot.simulation.person_tracker import PersonTracker

    tracker = PersonTracker(conf=0.30, device="cpu", imgsz=352, face_index=None)  # == patrol._fast_tracker defaults
    gates = dict(min_conf=0.45, min_keypoints=4, min_age_ms=250)                  # == patrol's Perception(...) gates
    return tracker, gates, plausible_people, TargetIdentifier("Jeanine", "red")


def annotate(cv2, img, raw, accepted):
    out = img.copy()
    accepted_ids = {id(t) for t in accepted}
    for t in raw:
        x1, y1, x2, y2 = (int(v) for v in t["box"])
        ok = id(t) in accepted_ids
        named = ok and t.get("identity")
        colour = (60, 60, 230) if named else (80, 200, 80) if ok else (160, 160, 160)
        label = f"{t['identity']['name']} {t['identity']['score']:.2f}" if named else f"person {t['conf']:.2f}" + ("" if ok else " (gated)")
        label += f" | {t['posture']}"
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)
        cv2.rectangle(out, (x1, max(0, y1 - 16)), (min(WIDTH, x1 + 8 * len(label)), y1), colour, -1)
        cv2.putText(out, label, (x1 + 2, max(11, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def contact_sheet(Image, paths, dest, columns=6, thumb=(320, 240)):
    rows = math.ceil(len(paths) / columns)
    sheet = Image.new("RGB", (columns * thumb[0], rows * thumb[1]), (18, 18, 18))
    for i, path in enumerate(paths):
        sheet.paste(Image.open(path).resize(thumb), ((i % columns) * thumb[0], (i // columns) * thumb[1]))
    sheet.save(dest, quality=90)


def render_variant(variant, frames, out, perception=True):
    import mujoco
    import numpy as np
    from PIL import Image

    out.mkdir(parents=True, exist_ok=True)
    model = apartment.load_model(variant)
    data = mujoco.MjData(model)
    key_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, "apartment_start")
    renderer = mujoco.Renderer(model, height=HEIGHT, width=WIDTH)
    path = sample_path(apartment.PATHS[variant], frames)

    cv2 = tracker = None
    if perception:
        import cv2
        tracker, gates, plausible_people, identifier = build_perception()

    front, annotated, per_frame = [], [], []
    for k, (x, y, yaw, travelled) in enumerate(path):
        pose_robot(mujoco, model, data, key_id, x, y, yaw, travelled)
        renderer.update_scene(data, camera=apartment.CAMERA["name"])
        rgb = renderer.render()
        dest = out / f"front_{k:03d}.jpg"
        Image.fromarray(rgb).save(dest, quality=92)
        front.append(dest)
        record = {"frame": k, "pose": {"x": round(x, 3), "y": round(y, 3), "yaw_deg": round(math.degrees(yaw), 1)}}
        if perception:
            bgr = np.ascontiguousarray(rgb[:, :, ::-1])
            now_ms = 1_000 + k * FRAME_MS
            raw = tracker.update(bgr, now_ms=now_ms)
            accepted = plausible_people(raw, now_ms=now_ms, **gates)
            identifier.apply(bgr, accepted)
            record["raw_detections"] = len(raw)
            record["people"] = [{"track_id": t["track_id"], "conf": t["conf"], "posture": t["posture"],
                                 "box": [round(v) for v in t["box"]], "shirt_red_fraction": t.get("shirt_colour_fraction"),
                                 "identity": (t.get("identity") or {}).get("name")} for t in accepted]
            note = out / f"annotated_{k:03d}.jpg"
            cv2.imwrite(str(note), annotate(cv2, bgr, raw, accepted), [cv2.IMWRITE_JPEG_QUALITY, 92])
            annotated.append(note)
        per_frame.append(record)

    # Third-person beauty shot with the dog mid-walk.
    x, y, yaw, travelled = path[min(len(path) - 1, int(len(path) * 0.62))]
    pose_robot(mujoco, model, data, key_id, x, y, yaw, travelled)
    wide = mujoco.Renderer(model, height=960, width=1280)
    wide.update_scene(data, camera="overview")
    Image.fromarray(wide.render()).save(out / "overview.jpg", quality=93)
    wide.close()
    renderer.close()

    contact_sheet(Image, front, out / "contact_sheet.jpg")
    summary = {"variant": variant, "frames": frames, "camera": apartment.CAMERA, "resolution": [WIDTH, HEIGHT],
               "motion": "kinematic_scripted_path_no_physics", "evidence": "rendered_image_inference_direct_mujoco"}
    if perception:
        contact_sheet(Image, annotated, out / "contact_sheet_annotated.jpg")
        with_person = [r for r in per_frame if r["people"]]
        named = [r for r in per_frame if any(p["identity"] for p in r["people"])]
        fractions = [p["shirt_red_fraction"] for r in per_frame for p in r["people"] if p["shirt_red_fraction"] is not None]
        summary.update({
            "tracker": {"checkpoint": tracker.model_path, "conf": tracker.conf, "imgsz": tracker.imgsz, "device": tracker.device},
            "gates": gates, "identifier": {"name": identifier.name, "colour": identifier.colour, "min_fraction": identifier.min_fraction,
                                           "method": "shirt_colour_match_not_identification"},
            "frames_with_raw_detection": sum(1 for r in per_frame if r["raw_detections"]),
            "frames_with_accepted_person": len(with_person), "frames_identified_as_jeanine": len(named),
            "first_identified_frame": named[0]["frame"] if named else None,
            "postures_seen": sorted({p["posture"] for r in per_frame for p in r["people"]}),
            "max_shirt_red_fraction": max(fractions) if fractions else None,
        })
    (out / "detections.json").write_text(json.dumps({"summary": summary, "frames": per_frame}, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--out", type=Path, default=ROOT / "output/simulation/apartment")
    parser.add_argument("--variant", choices=(*apartment.VARIANTS, "all"), default="all")
    parser.add_argument("--no-perception", action="store_true", help="render only; skip YOLO pose + identifier")
    parser.add_argument("--rebuild", action="store_true", help="rewrite the scene XMLs from the builder first")
    args = parser.parse_args()
    if not 2 <= args.frames <= 600:
        parser.error("--frames must be between 2 and 600")
    if args.rebuild:
        apartment.write_all()
    variants = apartment.VARIANTS if args.variant == "all" else (args.variant,)
    for variant in variants:
        print(json.dumps(render_variant(variant, args.frames, args.out / variant, perception=not args.no_perception)))


if __name__ == "__main__":
    main()
