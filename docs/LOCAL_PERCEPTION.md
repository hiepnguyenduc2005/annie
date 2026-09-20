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

## Face matching for enrolled people (optional)

`robot/simulation/face_id.py` lets the person tracker say which *enrolled,
consenting* person a track looks like ("track 4 looks like Ellis"). It runs
entirely on this machine. `FaceIndex.identify(jpeg, box_xyxy)` embeds the
largest face inside the person box and returns `{'name', 'score'}` for the best
enrolled match, or `None` when the cosine similarity is below 0.45, no usable
face (>= 20 px) is visible, or nobody is enrolled.
`PersonTracker(face_index=FaceIndex().load('.data/faces/index.json'))` adds
`identity` to every track: looked up on a track's first update and then at most
once per 10 updates, kept for the life of the track once known, and dropped
with the stale track. A face-matching error is recorded in
`tracker.face_id_error` and never interrupts person or posture tracking.
Without `face_index` the tracker output is unchanged.

**A match is a similarity estimate, not an identification.** Scores are raw
cosine values, not calibrated probabilities. Look-alikes, poor light, small or
turned faces cause misses and false matches, and a false match stays on the
track until it goes stale. When person boxes overlap, the largest face inside
the box is used, so a closer bystander can be matched to the wrong track. Do
not gate safety behaviour, access, or anything irreversible on a name.

**Consent.** Enrol only people who explicitly agreed; never the resident by
default. Photos go in the git-ignored `.data/faces/<name>/*.jpg`; the index
`.data/faces/index.json` holds face templates (biometric data, written 0600).
Neither is committed, uploaded or logged. `forget <name>` removes a person.

```sh
.cache/dimos/.venv/bin/python -m robot.simulation.face_id setup            # once, online
.cache/dimos/.venv/bin/python -m robot.simulation.face_id enroll Ellis     # reads .data/faces/Ellis/*.jpg
.cache/dimos/.venv/bin/python -m robot.simulation.face_id who photo.jpg
.cache/dimos/.venv/bin/python -m robot.simulation.face_id forget Ellis
```

**Licence and provenance** (forwarded for the separate compliance review):
`insightface` 2.0 from PyPI (MIT library code,
https://github.com/deepinsight/insightface) on `onnxruntime` 1.30.0 CPU. Model
pack `buffalo_sc` (SCRFD-500MF `det_500m.onnx` SHA-256 `5e4447f5...b4ea3a`,
MobileFaceNet/WebFace600K `w600k_mbf.onnx` SHA-256 `9cc6e4a7...19eb4f`; full
hashes are pinned in the module and checked at load) from
https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_sc.zip,
cached under `.cache/insightface/`. InsightFace's pretrained weights are for
**non-commercial research use only**; a commercial deployment needs a licence
from InsightFace or a different model. The runtime never downloads: a missing
pack is reported and `setup` fetches it.

**Measured (2026-09-19, Apple M1 Max, onnxruntime CPU, warm, while other
agents held the machine at load average ~49, so these are upper bounds):**

| Step | Latency |
| --- | --- |
| Model load | 0.7-1.0 s |
| First lookup after load | ~190 ms |
| `identify` on a person box with a face (JPEG decode + detect at 640 + embed) | 65-86 ms median, 92-186 ms worst |
| `identify` on a person box with no face | 27 ms median |
| `PersonTracker.update` (yolo11n-pose, 4 people) without / with `face_index` | 124 ms / 122 ms median; 322 ms worst on a frame that looked up several new tracks |

Real-path smoke check (not a test; Ultralytics sample photos as two stand-in
"people", one enrolled crop each): on mirrored, darkened, JPEG-q60 copies at
1.0x / 0.75x / 0.5x scale through `PersonTracker(face_index=...)`, the enrolled
`zidane.jpg` person scored 0.94 / 0.93 / 0.90 and the enrolled `bus.jpg` person
0.85 / 0.78 / 0.58 (face ~20 px wide at 0.5x, close to the threshold); every
other person in both photos stayed `None`, and different people scored ~0.0
against each other. The held-out views derive from the same two photos, so this
shows the path works, not recognition accuracy across days, poses or cameras.

Tests (fakes only, no model): `.venv/bin/python -m pytest robot/simulation/tests/test_face_id.py -q`.
