"""Household objects seen by the dog, placed in the room: "phone last seen by the couch at 09:42".

The pose tracker (`robot/simulation/person_tracker.py`) owns people. `ObjectDetector` finds a
curated set of COCO things with the Ultralytics YOLO11n detection checkpoint, gives each one an
id that survives across frames (label + box overlap, no ByteTrack), and `place` estimates where
it is in the odometry frame so `sightings` can go straight into
`robot.dog.memory.spacetime.SpacetimeRecorder.record_people`.

Measured vs estimated. Boxes are 2-D image measurements in the pixels of the frame passed in;
`conf` is a raw model score, not a calibrated probability; `label` is a model guess from 80
COCO classes, not an identification (a tablet is a "laptop", any dark slab is a "phone" or a
"remote"). `x, y, z` are ESTIMATES: bearing is linear in the box centre over `hfov_deg` (the same
approximation `robot.dog.runtime.patrol` uses for people, so both land consistently on the map,
but not a lens model), distance is either the LiDAR front-sector range (`depth == "lidar"`: the nearest
obstacle ahead, which may be the wall behind a small object or the table under it) or an
apparent-size prior (`depth == "prior"`: tens of percent off, worse for occluded or flat-lying
things), and `z` is a per-label guess at where such a thing usually sits. Good enough for "near
the couch", not for grasping. `object_id` names an IMAGE-SPACE track: it breaks when the dog
turns faster than the boxes overlap between two runs, so memory should key on label + position,
not on the id alone. `hits` counts the runs an id has been seen in; requiring `hits >= 2` before
remembering a sighting drops one-frame hallucinations.

False positives are real: on the one robot-camera render tried here the chair was right (0.85)
but a person lying on the floor was ALSO boxed as a "couch" at 0.69, and the bed and table were
missed. Pass the pose tracker's boxes through `not_people` before remembering anything.

Rate: the caller owns the rate limit (run `detect` every Nth frame or every ~1 s). Measured on
this Mac (Apple M1 Max, macOS 26.5.1, CPU, torch 2.7.1, ultralytics 8.4.150, 2026-09-20, 30 warm
runs per image after 5 warm-up runs, `detect` end to end including the id tracker; images:
ultralytics `bus.jpg`, `zidane.jpg` and a 640x480 robot-camera render):
    imgsz 416 (default), 480 px wide frames as on the live path: mean 24-30 ms, p95 28-33 ms
    imgsz 416, full-size images (810x1080, 1280x720, 640x480):   mean 25-30 ms, p95 29-37 ms
    imgsz 320: mean 17-21 ms, p95 20-24 ms;  imgsz 640: mean 40-54 ms, p95 44-55 ms
    imgsz 416 while the pose tracker (imgsz 352) runs flat out in another thread: mean 36 ms,
    p95 40 ms, and the pose step itself slows from 26 to 34 ms while the two overlap.
So one run per second takes about 3 % of a thread's time; run inline in the tracker thread it
delays that one frame by ~30 ms (roughly half a frame interval at 14 fps). CPU on purpose: on Apple MPS the
Ultralytics NMS step stalls on real frames (see `robot.dog.runtime.patrol._fast_tracker`).

Model provenance: `yolo11n.pt`, Ultralytics YOLO11n COCO detection (80 classes, 2.6 M params),
from the `ultralytics/assets` GitHub release `v8.4.0`
(https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt), sha256
0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1. Code and weights are
AGPL-3.0 (https://github.com/ultralytics/ultralytics/blob/main/LICENSE); trained on COCO 2017.
The runtime never downloads: it looks for the file under `.cache/` and the repo root and raises
if it is missing. `ObjectDetector.setup()` is the one place that fetches it (to
`.cache/ultralytics/yolo11n.pt`). Ultralytics, cv2 and numpy are imported only on the real-model
path and in `annotate`, so an injected `predictor` needs none of them.

Open-vocabulary mode (`ObjectDetector(vocabulary=HOME_VOCABULARY)` or `ObjectDetector.open_vocab()`)
swaps in Ultralytics YOLO-World v2 small so the dog also has doors, walls, windows, stairs and other
things COCO has no class for. The vocabulary is a list of text PROMPTS; `ALIASES` folds prompts that
the model cannot tell apart onto one LABEL ("doorway"/"open door" -> "door", "desk" -> "table",
"sofa" -> "couch", ...), because the same closed door scored "doorway 0.72" at imgsz 320 and
"door 0.81" at 416, and an id track keyed on a flipping label never reaches `hits >= 2`. The prompt
that fired rides along as `prompt`. Same-label boxes overlapping by `DEDUPE_IOU` collapse to the best
one. NMS stays class-aware on purpose: agnostic NMS lets a below-threshold "refrigerator 0.32" delete
the "door 0.20" under it.

Thresholds are per label (`label_min_conf`, default `HOME_MIN_CONF`, else `min_conf`). Measured
2026-09-20 through `detect` in this configuration, imgsz 320 / 416, CPU. Doors: 11 public interior
photos (Wikimedia Commons, human eye height, NOT the dog's camera) gave a best door score of
0.11-0.72 / 0.14-0.81 (10 of 11 / 11 of 11 reach 0.12), while 11 real door-free dog frames and 3
simulator renders gave nothing above 0.03, hence 0.12, and 416 is the better size for doors. One
more real frame, the camera 4 cm from a white surface with a door frame at the edge of view, gave
"door" 0.17 over the whole surface at 320 only: possibly a real door, not verified. Walls score
0.08-0.26 when plainly in view, windows 0.15-0.64, tables 0.08-0.43 (a table seen edge-on from dog
height is often missed), chairs 0.30-0.86. False positives seen on the real frames: "cup" 0.31,
"refrigerator" 0.32 (that blocked frame), "television" 0.12-0.20, "trash can" 0.12-0.17, so small
things keep 0.35. "stairs" scored 0.04 on the one staircase photo tried: do NOT read a missing
"stairs" as "no stairs"; LiDAR/cliff handling owns that. The door numbers are provisional until
re-measured from the dog's own camera with a door in view.

Rate: same Mac and method as above, 30 warm runs per image on 480x270 real dog frames, but taken
while the machine was heavily loaded by unrelated work (load average ~500), so treat these as
upper bounds and the means/p95 as inflated by stalls of up to 2 s. CPU, two samples:
    imgsz 320: p50 37 / 49 ms, mean 49 / 67 ms;   imgsz 416: p50 60 / 70 ms, mean 153 / 83 ms
(the raw model earlier the same night: p50 34-45 ms at 320, 48-61 ms at 416). About 2x YOLO11n, fine
at ~1 Hz. The first `detect` in a process costs 5-9 s (import, load, warm-up): build and warm the
detector off the control thread. MPS ran (p50 32 / 35 ms, same detections) but printed the
Ultralytics "NMS time limit 2.050s exceeded" warning once during its first call, the stall noted
above, which silently truncates that frame's detections: CPU stays the default.

The text encoder (CLIP ViT-B/32, 338 MB, https://github.com/ultralytics/CLIP, MIT) is needed only to
turn the vocabulary into embeddings: ~11 s on CPU. `ObjectDetector.setup(vocabulary=...)` does that
once and saves a 26 MB "baked" checkpoint (`.cache/yolo/yolov8s-worldv2-vocab-<hash>.pt`) that loads
in well under a second with no `clip` import. The runtime prefers the baked file, and otherwise
calls `set_classes` exactly once at model load. It never lets Ultralytics auto-install `clip` into a
shared venv: a missing `clip` is an `ObjectDetectorError` carrying the install command.
Provenance: `yolov8s-worldv2.pt`, `ultralytics/assets` release `v8.4.0`
(https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8s-worldv2.pt), sha256
9b2c17ab6124a913e9b3a5c170617920d91b0f01111a8479da69f00e2cf27792, AGPL-3.0.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
from pathlib import Path
from typing import Callable, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKPOINT = "yolo11n.pt"
MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt"
SETUP_PATH = ".cache/ultralytics/yolo11n.pt"
SEARCH_DIRS = (".cache/ultralytics", ".cache/yolo", ".cache/models", ".cache", ".")  # then .cache/*/

# COCO-80 class id -> our label (COCO name in the comment where we shorten it). No person: the pose tracker owns people.
CLASSES = {
    67: "phone",      # cell phone
    41: "cup",
    39: "bottle",
    65: "remote",
    73: "book",
    63: "laptop",
    56: "chair",
    57: "couch",
    59: "bed",
    60: "table",      # dining table
    62: "tv",
    24: "backpack",
    26: "handbag",
    58: "plant",      # potted plant
    74: "clock",
    75: "vase",
    61: "toilet",
}

# distance_m ~= DIST_K[label] / (box height as a fraction of the frame height). For a pinhole camera
# K = visible height [m] * focal length / frame height, which is ~0.75 * height for the Go2's ~100 deg, 16:9
# front camera. Heights are typical VISIBLE heights (a phone is as often flat as upright), so this is a rough prior.
DIST_K = {"phone": 0.09, "cup": 0.075, "bottle": 0.19, "remote": 0.075, "book": 0.15, "laptop": 0.19,
          "chair": 0.68, "couch": 0.64, "bed": 0.45, "table": 0.56, "tv": 0.45, "backpack": 0.34,
          "handbag": 0.22, "plant": 0.45, "clock": 0.22, "vase": 0.22, "toilet": 0.56}
# Where the middle of such a thing usually is above the floor [m]: a guess for the 3-D view, not a measurement.
Z_PRIOR_M = {"phone": 0.6, "cup": 0.7, "bottle": 0.7, "remote": 0.5, "book": 0.6, "laptop": 0.7,
             "chair": 0.45, "couch": 0.4, "bed": 0.4, "table": 0.4, "tv": 1.2, "backpack": 0.3,
             "handbag": 0.4, "plant": 0.5, "clock": 1.6, "vase": 0.8, "toilet": 0.4}
# ---- open-vocabulary mode (YOLO-World) ----
WORLD_CHECKPOINT = "yolov8s-worldv2.pt"
WORLD_MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8s-worldv2.pt"
WORLD_SETUP_PATH = ".cache/yolo/yolov8s-worldv2.pt"
CLIP_REQUIREMENT = "git+https://github.com/ultralytics/CLIP.git@a13192f8cb767260d7dfd98c843b0716593169e7"
CLIP_INSTALL = f"uv pip install --python .cache/dimos/.venv/bin/python '{CLIP_REQUIREMENT}'"

# Text prompts given to the model, in class-id order. Several prompts may share one label (ALIASES).
HOME_VOCABULARY = ("door", "doorway", "open door", "wall", "window", "table", "desk", "chair", "sofa", "couch", "bed",
                   "television", "stairs", "potted plant", "backpack", "handbag", "suitcase", "bottle", "cup", "laptop",
                   "cell phone", "remote", "book", "refrigerator", "microwave", "sink", "toilet", "trash can", "shoes",
                   "walking cane", "wheelchair", "pill bottle")
# prompt -> label. The model flips between these prompts on the same thing, and the rest of the dog
# (memory, DIST_K, the COCO path) already says phone/couch/tv/plant/table.
ALIASES = {"doorway": "door", "open door": "door", "desk": "table", "sofa": "couch", "television": "tv",
           "cell phone": "phone", "potted plant": "plant"}
# Per-LABEL score floors measured on this model (module docstring); any other label uses `min_conf`.
HOME_MIN_CONF = {"door": 0.12, "wall": 0.10, "window": 0.20, "stairs": 0.15, "table": 0.20,
                 "chair": 0.25, "couch": 0.25, "bed": 0.25, "shoes": 0.25}
DEDUPE_IOU = 0.6                    # same label, boxes overlapping this much: one thing seen through two prompts

DIST_K.update({"door": 1.5, "wall": 1.8, "window": 0.9, "stairs": 1.1, "suitcase": 0.45, "refrigerator": 1.28,
               "microwave": 0.22, "sink": 0.19, "trash can": 0.38, "shoes": 0.075, "walking cane": 0.68,
               "wheelchair": 0.68, "pill bottle": 0.06})   # a door or wall usually overflows the frame: prefer LiDAR
Z_PRIOR_M.update({"door": 1.0, "wall": 1.2, "window": 1.4, "stairs": 0.5, "suitcase": 0.3, "refrigerator": 0.85,
                  "microwave": 1.0, "sink": 0.85, "trash can": 0.25, "shoes": 0.05, "walking cane": 0.45,
                  "wheelchair": 0.45, "pill bottle": 0.7})

MIN_DIST_M, MAX_DIST_M = 0.4, 6.0   # clamp for the size prior
CENTRED_FRAC = 0.18                 # |cx - 0.5| below this: the LiDAR front sector is looking at the box
CYAN_BGR = (255, 255, 0)


class ObjectDetectorError(RuntimeError):
    """Raised for a missing checkpoint, an unreadable frame, or inference failure."""


def find_checkpoint(name: str = CHECKPOINT) -> Path | None:
    """The first existing `name` under the known cache folders, `.cache/*/`, or the repo root. No network."""
    for d in SEARCH_DIRS:
        if (REPO_ROOT / d / name).is_file():
            return REPO_ROOT / d / name
    return next(iter(sorted((REPO_ROOT / ".cache").glob(f"*/{name}"))), None)


def label_for(prompt: str) -> str:
    """The label a vocabulary prompt is reported under (`ALIASES`, else the prompt itself)."""
    return ALIASES.get(prompt, prompt)


def baked_name(vocabulary: Iterable[str]) -> str:
    """File name of the checkpoint with `vocabulary` already embedded; the hash keys it to that exact prompt list."""
    digest = hashlib.sha256("\n".join(vocabulary).encode()).hexdigest()[:8]
    return f"{WORLD_CHECKPOINT[:-3]}-vocab-{digest}.pt"


def _open_yolo(path):
    """Load an Ultralytics checkpoint (COCO or YOLO-World; `YOLO` picks the class from the file)."""
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise ObjectDetectorError("ultralytics not installed; see robot/simulation/requirements-perception.txt") from exc
    try:
        return YOLO(str(path))
    except Exception as exc:  # noqa: BLE001 - bad checkpoint file
        raise ObjectDetectorError(f"cannot load checkpoint: {exc}") from exc


def _require_clip() -> None:
    """Fail BEFORE Ultralytics notices `clip` is missing: its fallback pip-installs into the running venv."""
    if importlib.util.find_spec("clip") is None:
        raise ObjectDetectorError(f"the CLIP text encoder is not installed, and it is needed to embed a vocabulary; "
                                  f"install it once with: {CLIP_INSTALL}  (or bake a checkpoint on a machine that has "
                                  "it: ObjectDetector.setup(vocabulary=...))")


def _model_names(model) -> list[str]:
    names = getattr(model, "names", None) or {}
    return [names[k] for k in sorted(names)] if isinstance(names, Mapping) else list(names)


def _embed_vocabulary(model, vocabulary) -> None:
    """The one `set_classes` call (CLIP load + text encoding, ~11 s on CPU). CLIP is dropped afterwards:
    it is ~340 MB the detector never uses again, and `save()` would otherwise pickle it into the checkpoint."""
    _require_clip()
    try:
        model.set_classes(list(vocabulary))
    except Exception as exc:  # noqa: BLE001 - not a YOLO-World checkpoint, CLIP weights unavailable, ...
        raise ObjectDetectorError(f"cannot set the vocabulary on this checkpoint: {exc}") from exc
    inner = getattr(model, "model", None)
    if inner is not None and getattr(inner, "clip_model", None) is not None:
        inner.clip_model = None


def _download(url: str, path: Path) -> None:
    if not path.is_file():
        from ultralytics.utils.downloads import safe_download
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_download(url=url, file=path, min_bytes=1e5)
    if not path.is_file():
        raise ObjectDetectorError(f"download failed: {url}")


def _bake(vocabulary: list[str], dest: str | None) -> str:
    """`setup(vocabulary=...)`: world weights + one `set_classes` -> a small checkpoint that needs no CLIP."""
    if dest is None:
        found = find_checkpoint(baked_name(vocabulary))
        if found is not None:
            return str(found)
        dest = str(Path(WORLD_SETUP_PATH).parent / baked_name(vocabulary))
    out = Path(dest) if Path(dest).is_absolute() else REPO_ROOT / dest
    if out.is_file():
        return str(out)
    _require_clip()  # before any download: fail fast, and never let Ultralytics auto-install it
    raw = find_checkpoint(WORLD_CHECKPOINT)
    if raw is None:
        raw = REPO_ROOT / WORLD_SETUP_PATH
        _download(WORLD_MODEL_URL, raw)
    model = _open_yolo(raw)
    _embed_vocabulary(model, vocabulary)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        model.save(str(out))
    except Exception as exc:  # noqa: BLE001
        raise ObjectDetectorError(f"cannot save the baked checkpoint: {exc}") from exc
    return str(out)


def _iou(a, b) -> float:
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def not_people(detections, person_boxes, max_iou=0.6) -> list[dict]:
    """Drop detections whose box nearly coincides with a person box (a lying person reads as a "couch").
    A person sitting ON a couch overlaps it far less than `max_iou`, so the couch stays."""
    return [d for d in detections if all(_iou(d["box"], [float(v) for v in pb]) < max_iou for pb in person_boxes)]


def place(detections, frame_w, frame_h, pose_xy, yaw, front_range_m=None, hfov_deg=100.0) -> list[dict]:
    """Copies of `detections` with estimated `x, y, z` (odometry frame, metres), `dist_m` and `depth`.

    `pose_xy`/`yaw` are the robot's odometry pose (metres / radians, counter-clockwise positive), so
    a box left of centre gets a positive bearing. `depth` is "lidar" when the box is centred and a
    finite positive `front_range_m` was given, else "prior" (size table, clamped to 0.4..6 m).
    """
    lidar_ok = front_range_m is not None and math.isfinite(front_range_m) and front_range_m > 0
    out = []
    for d in detections:
        x1, y1, x2, y2 = (float(v) for v in d["box"])
        cx = (x1 + x2) / 2.0 / float(frame_w)
        hfrac = max(1e-3, (y2 - y1) / float(frame_h))
        if lidar_ok and abs(cx - 0.5) < CENTRED_FRAC:
            dist, depth = float(front_range_m), "lidar"
        else:
            dist, depth = min(MAX_DIST_M, max(MIN_DIST_M, DIST_K.get(d["label"], 0.3) / hfrac)), "prior"
        ang = yaw + (0.5 - cx) * math.radians(hfov_deg)
        out.append({**d, "x": round(pose_xy[0] + dist * math.cos(ang), 3), "y": round(pose_xy[1] + dist * math.sin(ang), 3),
                    "z": Z_PRIOR_M.get(d["label"], 0.5), "dist_m": round(dist, 3), "depth": depth})
    return out


def sightings(placed, now_ms) -> list[dict]:
    """Placed detections shaped for `SpacetimeRecorder.record_people` (which keeps track_id, x, y, z,
    label, identity, posture and ignores the rest). `kind`, `conf`, `depth` and `t_ms` ride along for
    a memory consumer. Ids are strings ("obj-3") so they never collide with ByteTrack's integer person ids."""
    return [{"track_id": d["object_id"], "x": d["x"], "y": d["y"], "z": d["z"], "label": d["label"],
             "identity": None, "posture": None, "kind": "object", "conf": d.get("conf"),
             "depth": d.get("depth"), "t_ms": int(now_ms)}
            for d in placed if d.get("x") is not None and d.get("y") is not None]


def annotate(image_bgr, placed):
    """Draw thin cyan boxes with "label ~distance" on `image_bgr` IN PLACE and return it (live view)."""
    import cv2
    for d in placed:
        x1, y1, x2, y2 = (int(round(float(v))) for v in d["box"])
        cv2.rectangle(image_bgr, (x1, y1), (x2, y2), CYAN_BGR, 1)
        text = d["label"] + (f" ~{d['dist_m']:.1f}m" if d.get("dist_m") is not None else "")
        cv2.putText(image_bgr, text, (x1 + 2, max(10, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, CYAN_BGR, 1, cv2.LINE_AA)
    return image_bgr


class ObjectDetector:
    """Curated COCO detections, or an open vocabulary (`vocabulary=`), with stable ids.
    `detect` is stateful: call it from one thread."""

    CLASSES = CLASSES
    not_people = staticmethod(not_people)
    place = staticmethod(place)
    sightings = staticmethod(sightings)
    annotate = staticmethod(annotate)

    def __init__(self, *, labels: Iterable[str] | None = None, vocabulary: Iterable[str] | None = None,
                 model_path: str | None = None, min_conf: float = 0.35,
                 label_min_conf: Mapping[str, float] | None = None, imgsz: int = 416, device: str = "cpu",
                 predictor: Callable[[object], list[dict]] | None = None, iou_match: float = 0.3, stale_ms: int = 3000):
        """`vocabulary` (text prompts) selects open-vocabulary mode; without it this is the COCO detector.
        `labels` keeps a subset of LABELS in either mode. `label_min_conf` overrides `min_conf` per label;
        left as None it is `HOME_MIN_CONF` in open-vocabulary mode and empty in COCO mode."""
        if not 0.0 < min_conf < 1.0:
            raise ValueError("min_conf must be in (0, 1)")
        if vocabulary is None:
            self.vocabulary = None
            id_to_label = dict(CLASSES)
        else:
            self.vocabulary = [str(p).strip() for p in ([vocabulary] if isinstance(vocabulary, str) else vocabulary)]
            if not self.vocabulary or not all(self.vocabulary) or len(set(self.vocabulary)) != len(self.vocabulary):
                raise ValueError("vocabulary must be a non-empty list of distinct, non-blank prompts")
            id_to_label = {i: label_for(p) for i, p in enumerate(self.vocabulary)}
        known = set(id_to_label.values())
        wanted = known if labels is None else set(labels)
        if wanted - known:
            raise ValueError(f"unknown labels {sorted(wanted - known)}; choose from {sorted(known)}")
        self.classes = {cid: label for cid, label in id_to_label.items() if label in wanted}
        if label_min_conf is None:  # defaults never raise: a custom vocabulary simply lacks some of these labels
            floors = {k: v for k, v in HOME_MIN_CONF.items() if k in known} if self.vocabulary is not None else {}
        else:
            floors = {str(k): float(v) for k, v in label_min_conf.items()}
            if set(floors) - known:
                raise ValueError(f"label_min_conf has unknown labels {sorted(set(floors) - known)}; choose from {sorted(known)}")
            if not all(0.0 < v < 1.0 for v in floors.values()):
                raise ValueError("label_min_conf values must be in (0, 1)")
        self.label_min_conf = floors
        self.model_path = model_path  # None: find_checkpoint() at first use
        self.min_conf, self.imgsz, self.device = float(min_conf), int(imgsz), device
        # What the model itself is asked for: the lowest floor among the labels we keep. `detect` applies each label's own.
        self._conf_floor = min([self.label_min_conf.get(label, self.min_conf) for label in self.classes.values()]
                               or [self.min_conf])
        self.iou_match, self.stale_ms = float(iou_match), int(stale_ms)
        self._tracks: dict[int, dict] = {}  # n -> {"label", "box", "last_seen_ms", "hits"}
        self._next_id = 1
        self._model = None
        self._predictor = predictor if predictor is not None else self._predict

    @classmethod
    def open_vocab(cls, vocabulary: Iterable[str] = HOME_VOCABULARY, **kwargs) -> "ObjectDetector":
        """Open-vocabulary detector; `HOME_VOCABULARY` by default. Other arguments as in the constructor."""
        return cls(vocabulary=vocabulary, **kwargs)

    @staticmethod
    def setup(dest: str | None = None, vocabulary: Iterable[str] | None = None) -> str:
        """Deployment step only (network): the runtime never calls this.

        No `vocabulary`: fetch `yolo11n.pt` once. With no `dest`, an existing checkpoint is reused, else it
        goes to `.cache/ultralytics/yolo11n.pt`.
        With a `vocabulary`: fetch `yolov8s-worldv2.pt` if missing, embed the vocabulary (needs `clip`, which
        downloads CLIP ViT-B/32 on first use) and save the baked checkpoint the runtime prefers. An existing
        baked file for this exact vocabulary is reused. Returns the path of the file the runtime will load."""
        if vocabulary is not None:
            return _bake(list(vocabulary), dest)
        if dest is None:
            found = find_checkpoint()
            if found is not None:
                return str(found)
            dest = SETUP_PATH
        path = Path(dest) if Path(dest).is_absolute() else REPO_ROOT / dest
        _download(MODEL_URL, path)
        return str(path)

    def _checkpoint_path(self) -> Path | None:
        if self.model_path is not None:
            path = Path(self.model_path)
            return path if path.is_absolute() or path.is_file() else REPO_ROOT / path  # repo-relative, not cwd-relative
        if self.vocabulary is None:
            return find_checkpoint()
        return find_checkpoint(baked_name(self.vocabulary)) or find_checkpoint(WORLD_CHECKPOINT)

    def _load_model(self):
        path = self._checkpoint_path()
        if path is None or not path.is_file():
            name, how = (CHECKPOINT, "setup()") if self.vocabulary is None else (WORLD_CHECKPOINT, "setup(vocabulary=...)")
            raise ObjectDetectorError(f"checkpoint not found: {self.model_path or name} (looked in {', '.join(SEARCH_DIRS)}, "
                                      f".cache/*/); run ObjectDetector.{how} once with network access")
        model = _open_yolo(path)
        if self.vocabulary is not None and _model_names(model) != self.vocabulary:
            _embed_vocabulary(model, self.vocabulary)  # once per model load; a baked checkpoint skips it (and CLIP)
        return model

    def _predict(self, image) -> list[dict]:
        """Real-model path: one BGR array (or JPEG bytes) -> raw `{"class_id", "conf", "box"}` detections."""
        import cv2
        import numpy as np
        if self._model is None:
            self._model = self._load_model()
        if not isinstance(image, np.ndarray):
            image = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)  # BGR, as Ultralytics expects
        if image is None or image.ndim != 3:
            raise ObjectDetectorError("unreadable frame: need a BGR array or a decodable JPEG")
        try:
            boxes = self._model.predict(image, conf=self._conf_floor, classes=sorted(self.classes), verbose=False,
                                        device=self.device, imgsz=self.imgsz)[0].boxes
        except Exception as exc:  # noqa: BLE001 - inference failure is reportable
            raise ObjectDetectorError(f"inference failed: {exc}") from exc
        if boxes is None or len(boxes) == 0:
            return []
        cls, conf, xyxy = boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
        return [{"class_id": int(cls[i]), "conf": float(conf[i]), "box": [round(float(v), 1) for v in xyxy[i]]}
                for i in range(len(cls))]

    def detect(self, image_bgr, now_ms) -> list[dict]:
        """One frame -> `{"label", "conf", "box": [x1, y1, x2, y2], "object_id", "hits"}` for this frame only
        (open-vocabulary mode adds `"prompt"`, the vocabulary phrase that fired).

        A detection is kept when its score reaches its label's floor (`label_min_conf`, else `min_conf`).
        Ids: each detection takes the id of the unclaimed live track with the same label and the highest
        IoU above `iou_match` (best overlaps first, so the result does not depend on detection order);
        otherwise it gets a new "obj-N". A track unseen for more than `stale_ms` is forgotten, so the
        same thing reappearing later is a new id.
        """
        now_ms = int(now_ms)
        for n in [n for n, t in self._tracks.items() if now_ms - t["last_seen_ms"] > self.stale_ms]:
            del self._tracks[n]
        dets = []
        for raw in self._predictor(image_bgr):
            label, box = self.classes.get(int(raw["class_id"])), [float(v) for v in raw["box"]]
            if (label is None or float(raw["conf"]) < self.label_min_conf.get(label, self.min_conf)
                    or box[2] <= box[0] or box[3] <= box[1]):
                continue
            det = {"label": label, "conf": round(float(raw["conf"]), 4), "box": box}
            if self.vocabulary is not None:
                det["prompt"] = self.vocabulary[int(raw["class_id"])]
            dets.append(det)
        if self.vocabulary is not None:  # "door" and "doorway" both boxed the same door: keep the better score
            best: list[dict] = []
            for d in sorted(dets, key=lambda d: -d["conf"]):
                if all(k["label"] != d["label"] or _iou(k["box"], d["box"]) < DEDUPE_IOU for k in best):
                    best.append(d)
            dets = [d for d in dets if any(d is k for k in best)]  # original order
        pairs = sorted((-_iou(d["box"], t["box"]), i, n) for i, d in enumerate(dets)
                       for n, t in self._tracks.items() if t["label"] == d["label"])
        taken: set[int] = set()
        for neg_iou, i, n in pairs:
            if -neg_iou <= self.iou_match:
                break
            if "n" not in dets[i] and n not in taken:
                dets[i]["n"] = n
                taken.add(n)
        for d in dets:
            n = d.pop("n", None)
            if n is None:
                n, self._next_id = self._next_id, self._next_id + 1
            hits = self._tracks[n]["hits"] + 1 if n in self._tracks else 1
            self._tracks[n] = {"label": d["label"], "box": d["box"], "last_seen_ms": now_ms, "hits": hits}
            d["object_id"], d["hits"] = f"obj-{n}", hits
        return dets
