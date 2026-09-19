# Local perception (fast camera safety pathway)

This is the local person-detection adapter for the detect-people-and-stop demo
pathway. It is a measurement layer only: it never drives the robot and never
substitutes ground-truth scene metadata for real camera output.

## Component

`robot/simulation/local_perception.py` loads an Ultralytics YOLO checkpoint and runs
person-class (COCO id 0) detection on one observation frame at a time. Results
carry the viewer `frame_id`, person bounding boxes in original-image pixel
coordinates, raw model confidences, measured wall-clock latency, and a demo
stop recommendation (`stop_recommended`: true when any person above threshold
is in frame - intentionally conservative so the pathway is easy to verify).

Bounding boxes are 2-D image measurements. They do not imply calibrated 3-D
distance, and raw confidences are not calibrated probabilities. "Person close
in frame" is not "person within N meters".

## Dependencies and checkpoints

- Runtime: the existing DimOS virtualenv `.cache/dimos/.venv` already contains
  `ultralytics` 8.4.150 and `torch` 2.7.1 (MPS available on this Mac).
- Checkpoints: public Ultralytics release assets, cached under `.cache/yolo/`
  (git-ignored via the existing `.cache/` rule):
  - https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt
  - https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt
  (AGPL-3.0, Ultralytics YOLO11; provenance forwarded for the separate
  compliance review. The pose checkpoint is optional; the safety pathway uses
  the plain detector today.)

## Usage

```python
from robot.simulation.local_perception import (
    PersonDetector, default_checkpoint, fetch_viewer_observation,
)

detector = PersonDetector(default_checkpoint("detect"), device="cpu")
observation = fetch_viewer_observation("http://127.0.0.1:8766")
result = detector.detect_observation(observation)
print(result.as_dict())
```

The viewer must already be running for `fetch_viewer_observation`; otherwise
it raises `LocalPerceptionError("viewer unavailable: ...")` so callers can
report availability truthfully instead of failing silently.

## Measured performance (2026-09-19, Apple M-series, warm model)

| Frame | Device | imgsz | Detector latency | Detections |
| --- | --- | --- | --- | --- |
| YOLO11s, saved robot-front floor view | cpu | 640 | 72 ms warm | person 0.713 |
| YOLO11s, live turn/check-in run | cpu | 640 | 84–106 ms | person 0.789 |
| YOLO11n, live feet-first view | cpu | 640 | measured separately | **Missed person**; replaced by YOLO11s |

Notes:

- First YOLO11s CPU call took 1.93 s including initialization. Movement is
  inhibited while the worker warms or its result is stale. The threshold is
  0.4; model scores are not calibrated probabilities.
- The adapter converts Pillow RGB to Ultralytics ndarray BGR. Earlier tiny-model
  measurements made before that correction are not qualification evidence.
- `setup_perception.py` verifies checkpoint SHA-256
  `85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5`.

## Limits

- Raw COCO-person detector on synthetic renders: false positives on the
  resident scan and furniture occur (the bed scene yields a second 0.3-0.7
  box on the chair at higher imgsz). Scores are not calibrated; no clinical
  or medical claim is made anywhere in this pathway.
- No orientation inference: if the camera yaw is wrong (for example facing a
  wall), the detector truthfully reports zero persons and the stop pathway
  stays quiet. Frame orientation is the caller (viewer/locomotion) concern.
- This module intentionally has no robot control, no incident-threshold
  changes, and no persistence. The root-owned viewer wiring decides how
  `stop_recommended` maps to a stop command.

## Tests

```sh
.cache/dimos/.venv/bin/python -m pytest robot/simulation/tests/test_local_perception.py -q
```

The tests cover frame decoding, observation payload handling, viewer
unavailability reporting, checkpoint loading errors, box clamping,
empty-frame behavior (no person -> no stop), and frame-id preservation. They
run on CPU with the cached detector checkpoint; no network or GPU needed.
