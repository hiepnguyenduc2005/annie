"""Household objects seen by the dog, placed in the room: "phone last seen by the couch at 09:42".

The pose tracker (`robot/simulation/person_tracker.py`) owns people. `ObjectDetector` finds a
curated set of COCO things with the Ultralytics YOLO11n detection checkpoint, gives each one an
id that survives across frames (label + box overlap, no ByteTrack), and `place` estimates where
it is in the odometry frame so `sightings` can go straight into
`go2_spacetime.SpacetimeRecorder.record_people`.

Measured vs estimated. Boxes are 2-D image measurements in the pixels of the frame passed in;
`conf` is a raw model score, not a calibrated probability; `label` is a model guess from 80
COCO classes, not an identification (a tablet is a "laptop", any dark slab is a "phone" or a
"remote"). `x, y, z` are ESTIMATES: bearing is linear in the box centre over `hfov_deg` (the same
approximation `go2_patrol_greet` uses for people, so both land consistently on the map, but not a
lens model), distance is either the LiDAR front-sector range (`depth == "lidar"`: the nearest
obstacle ahead, which may be the wall behind a small object or the table under it) or an
apparent-size prior (`depth == "prior"`: tens of percent off, worse for occluded or flat-lying
things), and `z` is a per-label guess at where such a thing usually sits. Good enough for "near
the couch", not for grasping. `object_id` names an IMAGE-SPACE track: it breaks when the dog
turns faster than the boxes overlap between two runs, so memory should key on label + position,
not on the id alone. `hits` counts the runs an id has been seen in; requiring `hits >= 2` before
remembering a sighting drops one-frame hallucinations.

Rate: the caller owns the rate limit (run `detect` every Nth frame or every ~1 s). Measured on
this Mac (Apple M4 Pro, macOS 26.5, CPU, torch 2.7.1, ultralytics 8.4.150, imgsz 416, 2026-09-20,
30 warm runs each, `detect` end to end including the id tracker):
    MEASUREMENTS_PLACEHOLDER
CPU on purpose: on Apple MPS the Ultralytics NMS step stalls on real frames (see
`go2_patrol_greet._fast_tracker`).

Model provenance: `yolo11n.pt`, Ultralytics YOLO11n COCO detection (80 classes, 2.6 M params),
from the `ultralytics/assets` GitHub release `v8.4.0`
(https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt), sha256
0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1. Code and weights are
AGPL-3.0 (https://github.com/ultralytics/ultralytics/blob/main/LICENSE); trained on COCO 2017.
The runtime never downloads: it looks for the file under `.cache/` and the repo root and raises
if it is missing. `ObjectDetector.setup()` is the one place that fetches it (to
`.cache/ultralytics/yolo11n.pt`). Ultralytics, cv2 and numpy are imported only on the real-model
path and in `annotate`, so an injected `predictor` needs none of them.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Iterable

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


def _iou(a, b) -> float:
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


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
    """Curated COCO detections with stable ids. `detect` is stateful: call it from one thread."""

    CLASSES = CLASSES
    place = staticmethod(place)
    sightings = staticmethod(sightings)
    annotate = staticmethod(annotate)

    def __init__(self, *, labels: Iterable[str] | None = None, model_path: str | None = None, min_conf: float = 0.35,
                 imgsz: int = 416, device: str = "cpu", predictor: Callable[[object], list[dict]] | None = None,
                 iou_match: float = 0.3, stale_ms: int = 3000):
        if not 0.0 < min_conf < 1.0:
            raise ValueError("min_conf must be in (0, 1)")
        wanted = set(CLASSES.values()) if labels is None else set(labels)
        if wanted - set(CLASSES.values()):
            raise ValueError(f"unknown labels {sorted(wanted - set(CLASSES.values()))}; choose from {sorted(CLASSES.values())}")
        self.classes = {cid: label for cid, label in CLASSES.items() if label in wanted}
        self.model_path = model_path  # None: find_checkpoint() at first use
        self.min_conf, self.imgsz, self.device = float(min_conf), int(imgsz), device
        self.iou_match, self.stale_ms = float(iou_match), int(stale_ms)
        self._tracks: dict[int, dict] = {}  # n -> {"label", "box", "last_seen_ms", "hits"}
        self._next_id = 1
        self._model = None
        self._predictor = predictor if predictor is not None else self._predict

    @staticmethod
    def setup(dest: str | None = None) -> str:
        """Fetch `yolo11n.pt` once (network). With no `dest`, an existing checkpoint is reused, else it
        goes to `.cache/ultralytics/yolo11n.pt`. Deployment step only: the runtime never calls this."""
        if dest is None:
            found = find_checkpoint()
            if found is not None:
                return str(found)
            dest = SETUP_PATH
        path = Path(dest) if Path(dest).is_absolute() else REPO_ROOT / dest
        if not path.is_file():
            from ultralytics.utils.downloads import safe_download
            path.parent.mkdir(parents=True, exist_ok=True)
            safe_download(url=MODEL_URL, file=path, min_bytes=1e5)
        if not path.is_file():
            raise ObjectDetectorError(f"download failed: {MODEL_URL}")
        return str(path)

    def _load_model(self):
        if self.model_path is None:
            path = find_checkpoint()
        else:
            path = Path(self.model_path)
            if not path.is_absolute() and not path.is_file():
                path = REPO_ROOT / path  # repo-relative, not cwd-relative
        if path is None or not path.is_file():
            raise ObjectDetectorError(f"checkpoint not found: {self.model_path or CHECKPOINT} (looked in {', '.join(SEARCH_DIRS)}, "
                                      ".cache/*/); run ObjectDetector.setup() once with network access")
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise ObjectDetectorError("ultralytics not installed; see robot/simulation/requirements-perception.txt") from exc
        try:
            return YOLO(str(path))
        except Exception as exc:  # noqa: BLE001 - bad checkpoint file
            raise ObjectDetectorError(f"cannot load checkpoint: {exc}") from exc

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
            boxes = self._model.predict(image, conf=self.min_conf, classes=sorted(self.classes), verbose=False,
                                        device=self.device, imgsz=self.imgsz)[0].boxes
        except Exception as exc:  # noqa: BLE001 - inference failure is reportable
            raise ObjectDetectorError(f"inference failed: {exc}") from exc
        if boxes is None or len(boxes) == 0:
            return []
        cls, conf, xyxy = boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
        return [{"class_id": int(cls[i]), "conf": float(conf[i]), "box": [round(float(v), 1) for v in xyxy[i]]}
                for i in range(len(cls))]

    def detect(self, image_bgr, now_ms) -> list[dict]:
        """One frame -> `{"label", "conf", "box": [x1, y1, x2, y2], "object_id", "hits"}` for this frame only.

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
            if label is None or float(raw["conf"]) < self.min_conf or box[2] <= box[0] or box[3] <= box[1]:
                continue
            dets.append({"label": label, "conf": round(float(raw["conf"]), 4), "box": box})
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
