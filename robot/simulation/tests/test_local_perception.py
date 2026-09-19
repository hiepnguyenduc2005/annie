import base64
import io

import numpy as np
import pytest
from PIL import Image

from robot.simulation.local_perception import (
    LocalPerceptionError,
    PersonDetector,
    default_checkpoint,
    decode_jpeg_b64,
    load_observation_frame,
)


def jpeg_b64_of(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def observation(frame_id="f-1", jpeg_b64=""):
    return {"frame_id": frame_id, "jpeg_b64": jpeg_b64}


def test_decode_rejects_non_image_payload():
    bad = base64.b64encode(b"not-an-image").decode()
    with pytest.raises(LocalPerceptionError):
        decode_jpeg_b64(bad)


def test_load_observation_frame_requires_payload():
    with pytest.raises(LocalPerceptionError):
        load_observation_frame({"frame_id": "x"})
    with pytest.raises(LocalPerceptionError):
        load_observation_frame("not-a-dict")


def test_fetch_viewer_observation_reports_unavailable():
    from robot.simulation.local_perception import fetch_viewer_observation

    with pytest.raises(LocalPerceptionError, match="viewer unavailable"):
        fetch_viewer_observation("http://127.0.0.1:1", timeout_s=0.05)


def test_detector_requires_existing_checkpoint():
    with pytest.raises(LocalPerceptionError, match="checkpoint not found"):
        PersonDetector("/nonexistent/yolo.pt")


def test_detector_rejects_bad_frame():
    detector = PersonDetector(default_checkpoint("detect"), device="cpu")
    with pytest.raises(LocalPerceptionError, match="HxWx3"):
        detector.detect_array(np.zeros((5, 5), dtype=np.uint8))


def test_detect_empty_frame_yields_no_person_and_no_stop():
    detector = PersonDetector(default_checkpoint("detect"), device="cpu")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    result = detector.detect_array(frame, frame_id="empty")
    assert result.person_count == 0
    assert result.persons == []
    assert result.stop_recommended is False
    assert result.frame_id == "empty"
    assert [result.source_width, result.source_height] == [640, 480]
    payload = result.as_dict()
    assert payload["person_count"] == 0
    assert payload["stop_recommended"] is False
    assert payload["latency_ms"] >= 0.0


def test_detect_observation_preserves_frame_id():
    detector = PersonDetector(default_checkpoint("detect"), device="cpu")
    frame = np.zeros((320, 240, 3), dtype=np.uint8)
    obs = observation(frame_id="abc-123", jpeg_b64=jpeg_b64_of(Image.fromarray(frame)))
    result = detector.detect_observation(obs)
    assert result.frame_id == "abc-123"


def test_boxes_clamped_to_image_bounds():
    detector = PersonDetector(default_checkpoint("detect"), device="cpu")
    frame = np.zeros((100, 80, 3), dtype=np.uint8)
    result = detector.detect_array(frame, frame_id="tiny")
    for box in result.persons:
        assert 0 <= box.x_min <= box.x_max <= 80
        assert 0 <= box.y_min <= box.y_max <= 100


def test_default_checkpoint_paths():
    assert default_checkpoint("detect").name == "yolo11s.pt"
    assert default_checkpoint("pose").name == "yolo11n-pose.pt"
    with pytest.raises(ValueError):
        default_checkpoint("bogus")
