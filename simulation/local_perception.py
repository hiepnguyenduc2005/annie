"""Local person detection for the fast camera safety pathway.

Loads an Ultralytics YOLO detector from an explicitly provided checkpoint and
returns person bounding boxes in original-image pixel coordinates plus timing.
Bounding-box position is a 2-D image measurement only: it never implies a
calibrated 3-D distance, and confidences are raw model scores, not calibrated
probabilities. Frame identity is preserved end to end.

Detection results are advisory measurements for a demo stop pathway; the
caller decides the stop policy. This module provides a conservative helper
(any confident person in frame) but does not control the robot.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PERSON_CLASS_ID = 0  # COCO class 0 = person in both yolo11n and yolo11n-pose


class LocalPerceptionError(RuntimeError):
    """Raised for missing checkpoints, unreadable frames, or inference failure."""


@dataclass(frozen=True)
class PersonBox:
    """One person detection in original-image pixel coordinates."""

    x_min: int
    y_min: int
    x_max: int
    y_max: int
    confidence: float

    def as_dict(self) -> dict:
        return {
            "x_min": self.x_min,
            "y_min": self.y_min,
            "x_max": self.x_max,
            "y_max": self.y_max,
            "confidence": round(self.confidence, 4),
        }


@dataclass(frozen=True)
class DetectionResult:
    """Person detections for one frame, with measured latency."""

    frame_id: str
    person_count: int
    persons: list[PersonBox]
    latency_ms: float
    source_width: int
    source_height: int
    model_path: str
    device: str
    stop_recommended: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "person_count": self.person_count,
            "persons": [p.as_dict() for p in self.persons],
            "latency_ms": round(self.latency_ms, 1),
            "source_size": [self.source_width, self.source_height],
            "model_path": self.model_path,
            "device": self.device,
            "stop_recommended": self.stop_recommended,
            "warnings": list(self.warnings),
        }


def decode_jpeg_b64(jpeg_b64: str) -> np.ndarray:
    """Decode one base64 JPEG observation payload to an RGB array."""
    from PIL import Image

    try:
        raw = base64.b64decode(jpeg_b64, validate=True)
        with Image.open(io.BytesIO(raw)) as img:
            return np.asarray(img.convert("RGB"))
    except Exception as exc:  # noqa: BLE001 - surface as domain error
        raise LocalPerceptionError(f"unreadable frame: {exc}") from exc


def load_observation_frame(observation: dict) -> np.ndarray:
    """Extract and decode the JPEG payload from a viewer observation dict."""
    jpeg_b64 = observation.get("jpeg_b64") if isinstance(observation, dict) else None
    if not isinstance(jpeg_b64, str) or not jpeg_b64:
        raise LocalPerceptionError("observation has no jpeg_b64 payload")
    return decode_jpeg_b64(jpeg_b64)


def fetch_viewer_observation(viewer_url: str, timeout_s: float = 5.0) -> dict:
    """Fetch one live observation from the running simulation viewer."""
    import httpx

    try:
        resp = httpx.get(f"{viewer_url.rstrip('/')}/observation", timeout=timeout_s)
        resp.raise_for_status()
        observation = resp.json()
    except Exception as exc:  # noqa: BLE001 - availability must be reportable
        raise LocalPerceptionError(f"viewer unavailable: {exc}") from exc
    if not isinstance(observation, dict) or "jpeg_b64" not in observation:
        raise LocalPerceptionError("viewer returned no observation frame")
    return observation


class PersonDetector:
    """Ultralytics YOLO person detector for the camera safety pathway."""

    def __init__(
        self,
        model_path: str | Path,
        device: str = "mps",
        confidence_threshold: float = 0.4,
        image_size: int = 640,
    ):
        if not 0.0 < confidence_threshold < 1.0:
            raise ValueError("confidence_threshold must be in (0, 1)")
        self.model_path = str(model_path)
        self.device = device
        self.confidence_threshold = float(confidence_threshold)
        self.image_size = int(image_size)
        self._model = self._load_model()

    def _load_model(self):
        path = Path(self.model_path)
        if not path.is_file():
            raise LocalPerceptionError(f"checkpoint not found: {self.model_path}")
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise LocalPerceptionError(
                "ultralytics not installed; see docs/LOCAL_PERCEPTION.md"
            ) from exc
        try:
            return YOLO(str(path))
        except Exception as exc:  # noqa: BLE001 - bad checkpoint file
            raise LocalPerceptionError(f"cannot load checkpoint: {exc}") from exc

    def detect_array(self, frame: np.ndarray, frame_id: str = "array") -> DetectionResult:
        """Run detection on an RGB array; boxes are in frame pixel coordinates."""
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise LocalPerceptionError("frame must be an HxWx3 array")
        warnings: list[str] = []
        start = time.perf_counter()
        try:
            result = self._model(
                # Ultralytics ndarray input follows OpenCV's BGR convention;
                # our public input is RGB from Pillow/MuJoCo.
                np.ascontiguousarray(frame[:, :, ::-1]),
                verbose=False,
                device=self.device,
                conf=self.confidence_threshold,
                imgsz=self.image_size,
                classes=[PERSON_CLASS_ID],
            )[0]
        except Exception as exc:  # noqa: BLE001 - inference failure is reportable
            raise LocalPerceptionError(f"inference failed: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000.0
        persons: list[PersonBox] = []
        boxes = result.boxes
        for i in range(len(boxes)):
            x_min, y_min, x_max, y_max = (int(round(v)) for v in boxes.xyxy[i].tolist())
            persons.append(
                PersonBox(
                    x_min=max(0, x_min),
                    y_min=max(0, y_min),
                    x_max=min(int(frame.shape[1]), x_max),
                    y_max=min(int(frame.shape[0]), y_max),
                    confidence=float(boxes.conf[i]),
                )
            )
        persons.sort(key=lambda p: p.confidence, reverse=True)
        stop_recommended = bool(persons)  # demo policy: any confident person
        if result.orig_shape[:2] != frame.shape[:2]:
            warnings.append("model output shape differs from input frame")
        return DetectionResult(
            frame_id=frame_id,
            person_count=len(persons),
            persons=persons,
            latency_ms=latency_ms,
            source_width=int(frame.shape[1]),
            source_height=int(frame.shape[0]),
            model_path=Path(self.model_path).name,
            device=self.device,
            stop_recommended=stop_recommended,
            warnings=warnings,
        )

    def detect_observation(self, observation: dict) -> DetectionResult:
        """Detect in a viewer observation dict, preserving its frame_id."""
        frame = load_observation_frame(observation)
        frame_id = str(observation.get("frame_id", "unknown"))
        return self.detect_array(frame, frame_id=frame_id)


def default_checkpoint(kind: str = "detect") -> Path:
    """Return the cached checkpoint path; does not download."""
    name = {"detect": "yolo11s.pt", "pose": "yolo11n-pose.pt"}.get(kind)
    if name is None:
        raise ValueError("kind must be 'detect' or 'pose'")
    return Path(__file__).resolve().parents[1] / ".cache" / "yolo" / name
