"""Tracked person detections with a keypoint posture signal.

Wraps an Ultralytics YOLO pose checkpoint with ByteTrack so each person keeps
a track id across frames, classifies every detection with
``posture.classify_posture``, and counts consecutive lying frames per track
for the demo fall trigger. Boxes and keypoints are 2-D image measurements in
original-image pixels; confidences are raw model scores, not calibrated
probabilities; posture is a coarse image-space estimate, not a diagnosis.

The tracker reports measurements only: the caller owns any stop or incident
policy. Ultralytics, cv2 and numpy are imported only on the real-model path,
so an injected ``predictor`` needs none of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from robot.simulation.posture import LYING, classify_posture

PERSON_CLASS_ID = 0  # COCO class 0 = person in yolo11n-pose
STALE_TRACK_MS = 3000  # a track not seen for this long is dropped
REPO_ROOT = Path(__file__).resolve().parents[2]


class PersonTrackerError(RuntimeError):
    """Raised for missing checkpoints, unreadable frames, or inference failure."""


class PersonTracker:
    """Per-frame person tracks with posture and consecutive-lying counts."""

    def __init__(
        self,
        *,
        model_path: str = ".cache/yolo/yolo11n-pose.pt",
        conf: float = 0.4,
        device: str = "cpu",
        predictor: Callable[[bytes], list[dict]] | None = None,
    ):
        if not 0.0 < conf < 1.0:
            raise ValueError("conf must be in (0, 1)")
        self.model_path = str(model_path)
        self.conf = float(conf)
        self.device = device
        self.tracks: dict[int, dict] = {}
        self._model = None
        self._predictor = predictor if predictor is not None else self._predict

    def _load_model(self):
        path = Path(self.model_path)
        if not path.is_absolute() and not path.is_file():
            path = REPO_ROOT / path  # default is repo-relative, not cwd-relative
        if not path.is_file():
            raise PersonTrackerError(f"checkpoint not found: {self.model_path}")
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise PersonTrackerError(
                "ultralytics not installed; see robot/simulation/requirements-perception.txt"
            ) from exc
        try:
            return YOLO(str(path))
        except Exception as exc:  # noqa: BLE001 - bad checkpoint file
            raise PersonTrackerError(f"cannot load checkpoint: {exc}") from exc

    def _predict(self, jpeg_bytes: bytes) -> list[dict]:
        """Real-model path: decode one JPEG and run pose + ByteTrack on it."""
        import cv2
        import numpy as np

        if self._model is None:
            self._model = self._load_model()
        # cv2 decodes to BGR, which is what Ultralytics expects for ndarrays.
        image = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise PersonTrackerError("unreadable frame: not a decodable JPEG")
        try:
            result = self._model.track(
                image,
                persist=True,
                conf=self.conf,
                classes=[PERSON_CLASS_ID],
                tracker="bytetrack.yaml",
                verbose=False,
                device=self.device,
            )[0]
        except Exception as exc:  # noqa: BLE001 - inference failure is reportable
            raise PersonTrackerError(f"inference failed: {exc}") from exc

        boxes, keypoints = result.boxes, result.keypoints
        if boxes is None or keypoints is None or len(boxes) == 0:
            return []
        ids = boxes.id.tolist() if boxes.id is not None else [None] * len(boxes)
        xy = keypoints.xy.tolist()
        kp_conf = (
            keypoints.conf.tolist()
            if keypoints.conf is not None
            else [[0.0] * len(person) for person in xy]
        )
        return [
            {
                "track_id": int(ids[i]) if ids[i] is not None else None,
                "box": [round(float(v), 1) for v in boxes.xyxy[i].tolist()],
                "conf": float(boxes.conf[i]),
                "keypoints": xy[i],
                "kp_conf": kp_conf[i],
            }
            for i in range(len(boxes))
        ]

    def update(self, jpeg_bytes: bytes, *, now_ms: int) -> list[dict]:
        """Track one frame; returns the people detected in this frame only.

        Tracks missing from this frame stay in ``self.tracks`` until they
        have gone unseen for ``STALE_TRACK_MS``, so a brief detection dropout
        keeps ``first_seen_ms`` and ``lying_frames``. ``lying_frames`` counts
        consecutive updates in which the track was seen lying and resets to 0
        whenever it is seen with any other posture.
        """
        current: list[dict] = []
        seen: set[int] = set()
        for det in self._predictor(jpeg_bytes):
            posture = classify_posture(det["keypoints"], det["kp_conf"], tuple(det["box"]))
            lying = posture["posture"] == LYING
            track_id = det.get("track_id")
            # Untracked (or duplicate-id) detections carry no history.
            if track_id is None or track_id in seen:
                previous, track_id = None, None
            else:
                track_id = int(track_id)
                seen.add(track_id)
                previous = self.tracks.get(track_id)
            track = {
                "track_id": track_id,
                "box": [float(v) for v in det["box"]],
                "conf": round(float(det["conf"]), 4),
                "posture": posture["posture"],
                "torso_angle_deg": posture["torso_angle_deg"],
                "first_seen_ms": previous["first_seen_ms"] if previous else int(now_ms),
                "last_seen_ms": int(now_ms),
                "lying_frames": ((previous["lying_frames"] if previous else 0) + 1) if lying else 0,
            }
            if track_id is not None:
                self.tracks[track_id] = track
            current.append(dict(track))

        for track_id in [
            tid
            for tid, track in self.tracks.items()
            if now_ms - track["last_seen_ms"] >= STALE_TRACK_MS
        ]:
            del self.tracks[track_id]
        return current
