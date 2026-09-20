"""Fakes-only tests for robot.dog.perception.objects: no model, no network (one opt-in real-model smoke test)."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from robot.dog.perception import objects  # noqa: E402
from robot.dog.perception.objects import CLASSES, ObjectDetector, ObjectDetectorError, place, sightings  # noqa: E402

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

PHONE, CUP, COUCH, PERSON = 67, 41, 57, 0
W, H = 480, 270
IMG = None  # fakes never look at the image


def raw(class_id, box, conf=0.8):
    return {"class_id": class_id, "conf": conf, "box": list(box)}


def scripted(*frames):
    """A predictor that returns the next scripted frame of raw detections on every call."""
    it = iter(frames)
    return lambda image: next(it)


def det(label="phone", box=(220, 100, 260, 140), conf=0.8, object_id="obj-1"):
    return {"label": label, "conf": conf, "box": list(box), "object_id": object_id, "hits": 1}


# ---- detect: curation, confidence, ids ---------------------------------------------------------
def test_curated_classes_skip_people_and_cover_the_memory_vocabulary():
    assert PERSON not in CLASSES
    assert {"phone", "cup", "bottle", "remote", "book", "laptop", "chair", "couch", "bed", "table", "tv",
            "backpack", "handbag", "plant", "clock", "vase", "toilet"} == set(CLASSES.values())
    assert set(CLASSES.values()) <= set(objects.DIST_K) and set(CLASSES.values()) <= set(objects.Z_PRIOR_M)


def test_detect_keeps_curated_confident_well_formed_detections_only():
    d = ObjectDetector(predictor=scripted([raw(PHONE, (10, 10, 50, 50), 0.9), raw(PERSON, (0, 0, 100, 200), 0.99),
                                           raw(CUP, (60, 60, 90, 90), 0.34), raw(CUP, (60, 60, 90, 90), 0.35),
                                           raw(COUCH, (80, 80, 80, 120), 0.9)]))
    out = d.detect(IMG, 1000)
    assert [(o["label"], o["conf"]) for o in out] == [("phone", 0.9), ("cup", 0.35)]
    assert out[0] == {"label": "phone", "conf": 0.9, "box": [10.0, 10.0, 50.0, 50.0], "object_id": "obj-1", "hits": 1}


def test_label_subset_and_bad_arguments():
    d = ObjectDetector(labels=["phone"], predictor=scripted([raw(PHONE, (10, 10, 50, 50)), raw(CUP, (60, 60, 90, 90))]))
    assert [o["label"] for o in d.detect(IMG, 0)] == ["phone"]
    with pytest.raises(ValueError, match="unknown labels"):
        ObjectDetector(labels=["phone", "unicorn"], predictor=scripted())
    with pytest.raises(ValueError, match="min_conf"):
        ObjectDetector(min_conf=1.0, predictor=scripted())


def test_ids_are_stable_across_overlapping_frames_and_independent_of_detection_order():
    d = ObjectDetector(predictor=scripted(
        [raw(PHONE, (100, 100, 140, 140)), raw(CUP, (300, 100, 340, 150))],
        [raw(CUP, (304, 102, 344, 152)), raw(PHONE, (106, 104, 146, 144))],   # both moved a little, order swapped
        [raw(PHONE, (112, 108, 152, 148))]))
    first = {o["label"]: o["object_id"] for o in d.detect(IMG, 0)}
    second = {o["label"]: (o["object_id"], o["hits"]) for o in d.detect(IMG, 500)}
    assert first == {"phone": "obj-1", "cup": "obj-2"}
    assert second == {"phone": ("obj-1", 2), "cup": ("obj-2", 2)}
    assert [(o["object_id"], o["hits"]) for o in d.detect(IMG, 1000)] == [("obj-1", 3)]


def test_same_box_with_another_label_or_a_far_box_is_a_new_object():
    d = ObjectDetector(predictor=scripted([raw(PHONE, (100, 100, 140, 140))], [raw(objects_id("remote"), (100, 100, 140, 140))],
                                          [raw(PHONE, (300, 100, 340, 140))]))
    assert d.detect(IMG, 0)[0]["object_id"] == "obj-1"
    assert d.detect(IMG, 100)[0]["object_id"] == "obj-2"   # label differs: IoU 1.0 is not enough
    assert d.detect(IMG, 200)[0]["object_id"] == "obj-3"   # same label, no overlap


def test_two_same_label_objects_keep_their_own_ids_best_overlap_first():
    d = ObjectDetector(predictor=scripted(
        [raw(CUP, (100, 100, 140, 140)), raw(CUP, (130, 100, 170, 140))],
        [raw(CUP, (128, 100, 168, 140)), raw(CUP, (102, 100, 142, 140))]))  # the right cup is listed first now
    d.detect(IMG, 0)
    assert [o["object_id"] for o in d.detect(IMG, 300)] == ["obj-2", "obj-1"]


def test_ids_survive_a_short_dropout_and_expire_after_stale_ms():
    box = (100, 100, 140, 140)
    d = ObjectDetector(predictor=scripted([raw(PHONE, box)], [], [raw(PHONE, box)], [raw(PHONE, box)]))
    assert d.detect(IMG, 0)[0]["object_id"] == "obj-1"
    assert d.detect(IMG, 1500) == []
    assert d.detect(IMG, 3000)[0]["object_id"] == "obj-1"      # unseen for exactly 3 s: still the same id
    again = d.detect(IMG, 6001)[0]                             # unseen for more than 3 s: forgotten
    assert (again["object_id"], again["hits"]) == ("obj-2", 1)


def objects_id(label):
    return next(cid for cid, name in CLASSES.items() if name == label)


def test_not_people_drops_a_box_that_is_really_a_person_and_keeps_the_couch_they_sit_on():
    lying_person = (216, 182, 450, 352)                        # measured case: a lying person boxed as "couch" 0.69
    ghost = det("couch", box=(214, 180, 451, 354), object_id="obj-2")
    chair = det("chair", box=(342, 146, 438, 215), object_id="obj-1")
    assert objects.not_people([chair, ghost], [lying_person]) == [chair]
    sitting_person = (200, 60, 280, 250)                       # inside a wide couch box: IoU ~0.27
    couch = det("couch", box=(80, 120, 440, 260))
    assert ObjectDetector.not_people([couch], [sitting_person]) == [couch]
    assert objects.not_people([couch], []) == [couch]


# ---- place: geometry ---------------------------------------------------------------------------
def test_centred_box_with_lidar_range_lands_that_far_ahead_in_the_heading_direction():
    for yaw, want in ((0.0, (3.0, 1.0)), (math.pi / 2, (1.0, 3.0)), (math.pi, (-1.0, 1.0))):
        p = place([det(box=(220, 100, 260, 140))], W, H, (1.0, 1.0), yaw, front_range_m=2.0)[0]
        assert (p["x"], p["y"]) == pytest.approx(want, abs=1e-3)
        assert (p["depth"], p["dist_m"], p["z"]) == ("lidar", 2.0, objects.Z_PRIOR_M["phone"])


def test_off_centre_boxes_get_the_right_bearing_sign_and_use_the_size_prior():
    left = place([det(box=(20, 100, 60, 154))], W, H, (0.0, 0.0), 0.0, front_range_m=2.0)[0]
    right = place([det(box=(420, 100, 460, 154))], W, H, (0.0, 0.0), 0.0, front_range_m=2.0)[0]
    assert left["depth"] == right["depth"] == "prior"          # not centred: the LiDAR range is ignored
    assert left["y"] > 0 > right["y"] and left["x"] > 0 and right["x"] > 0   # left of the image is +y (counter-clockwise)
    assert left["y"] == pytest.approx(-right["y"], abs=1e-3)
    want = objects.DIST_K["phone"] / (54 / H)                  # 0.09 / 0.2 = 0.45 m
    assert left["dist_m"] == pytest.approx(want, abs=1e-3)
    bearing = math.atan2(left["y"], left["x"])
    assert bearing == pytest.approx((0.5 - 40 / W) * math.radians(100.0), abs=1e-3)


def test_hfov_scales_the_bearing():
    narrow = place([det(box=(20, 100, 60, 154))], W, H, (0.0, 0.0), 0.0, hfov_deg=60.0)[0]
    assert math.atan2(narrow["y"], narrow["x"]) == pytest.approx((0.5 - 40 / W) * math.radians(60.0), abs=1e-3)


def test_prior_depth_is_used_without_a_usable_range_and_clamps():
    centred = (220, 100, 260, 154)
    for bad in (None, float("inf"), float("nan"), 0.0, -1.0):
        assert place([det(box=centred)], W, H, (0, 0), 0.0, front_range_m=bad)[0]["depth"] == "prior"
    tiny = place([det("couch", box=(230, 130, 250, 132))], W, H, (0, 0), 0.0)[0]      # a sliver: prior says 86 m
    huge = place([det("phone", box=(200, 0, 280, 270))], W, H, (0, 0), 0.0)[0]        # fills the frame: prior says 0.09 m
    assert (tiny["dist_m"], huge["dist_m"]) == (objects.MAX_DIST_M, objects.MIN_DIST_M)
    assert place([], W, H, (0, 0), 0.0) == []


def test_place_does_not_mutate_its_input():
    d = det()
    place([d], W, H, (0, 0), 0.0, front_range_m=1.0)
    assert "x" not in d


# ---- sightings: recorder contract --------------------------------------------------------------
def test_sightings_shape():
    placed = place([det("phone", object_id="obj-7", conf=0.71)], W, H, (1.0, 2.0), 0.0, front_range_m=1.5)
    unplaced = det("cup", object_id="obj-8")
    out = sightings(placed + [unplaced], 1_758_361_320_000)
    assert out == [{"track_id": "obj-7", "x": 2.5, "y": 2.0, "z": 0.6, "label": "phone", "identity": None,
                    "posture": None, "kind": "object", "conf": 0.71, "depth": "lidar", "t_ms": 1_758_361_320_000}]


def test_recorder_accepts_sightings_next_to_a_person_with_the_same_number():
    spacetime = pytest.importorskip("robot.dog.memory.spacetime")
    rec = spacetime.SpacetimeRecorder()
    d = ObjectDetector(predictor=scripted([raw(PHONE, (220, 100, 260, 140))]))
    placed = d.place(d.detect(IMG, 0), W, H, (0.0, 0.0), 0.0, front_range_m=2.0)
    person = {"track_id": 1, "x": 1.0, "y": 0.5, "z": 0.9, "label": "person 1", "identity": None, "posture": "standing"}
    assert rec.record_people(100.0, [person] + d.sightings(placed, 100_000))
    stored = rec.latest()["people"]
    assert [(p["track_id"], p["label"]) for p in stored] == [(1, "person 1"), ("obj-1", "phone")]  # "obj-1" != 1
    assert (stored[1]["x"], stored[1]["y"], stored[1]["z"], stored[1]["posture"]) == (2.0, 0.0, 0.6, None)


# ---- annotate ----------------------------------------------------------------------------------
def test_annotate_draws_cyan_in_place_and_keeps_the_shape():
    img = np.zeros((H, W, 3), dtype=np.uint8)
    placed = place([det(box=(220, 100, 260, 140))], W, H, (0, 0), 0.0, front_range_m=2.0)
    out = objects.annotate(img, placed + [det("cup", box=(300, 40, 330, 80))])   # unplaced boxes draw too
    assert out is img and out.shape == (H, W, 3) and out.dtype == np.uint8
    assert tuple(int(v) for v in out[100, 240]) == objects.CYAN_BGR              # top edge of the phone box
    assert not out[120, 240].any()                                               # boxes are outlines, not filled
    assert objects.annotate(np.zeros((H, W, 3), dtype=np.uint8), []).sum() == 0


# ---- checkpoint handling -----------------------------------------------------------------------
def test_missing_checkpoint_is_a_clear_error_and_never_a_download(tmp_path, monkeypatch):
    monkeypatch.setattr(objects, "REPO_ROOT", tmp_path)
    assert objects.find_checkpoint() is None
    with pytest.raises(ObjectDetectorError, match=r"checkpoint not found.*setup\(\)"):
        ObjectDetector().detect(np.zeros((H, W, 3), dtype=np.uint8), 0)
    with pytest.raises(ObjectDetectorError, match="checkpoint not found: nope/yolo11n.pt"):
        ObjectDetector(model_path="nope/yolo11n.pt").detect(np.zeros((H, W, 3), dtype=np.uint8), 0)
    assert list(tmp_path.iterdir()) == []


def test_find_checkpoint_prefers_known_folders_then_any_cache_subfolder(tmp_path, monkeypatch):
    monkeypatch.setattr(objects, "REPO_ROOT", tmp_path)
    other = tmp_path / ".cache" / "somewhere" / "yolo11n.pt"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    assert objects.find_checkpoint() == other
    known = tmp_path / ".cache" / "ultralytics" / "yolo11n.pt"
    known.parent.mkdir(parents=True)
    known.write_bytes(b"x")
    assert objects.find_checkpoint() == known
    assert ObjectDetector.setup() == str(known)               # something is already there: no network


def test_unreadable_frame_is_reported(monkeypatch):
    d = ObjectDetector()
    monkeypatch.setattr(d, "_load_model", lambda: object())
    with pytest.raises(ObjectDetectorError, match="unreadable frame"):
        d.detect(b"not a jpeg", 0)


@pytest.mark.skipif(objects.find_checkpoint() is None, reason="yolo11n.pt not present; run ObjectDetector.setup()")
def test_real_model_smoke_on_the_ultralytics_sample_image():
    ultralytics = pytest.importorskip("ultralytics")
    sample = Path(ultralytics.__file__).parent / "assets" / "zidane.jpg"
    if not sample.is_file():
        pytest.skip("ultralytics sample image not shipped")
    d = ObjectDetector(min_conf=0.25)
    img = cv2.imread(str(sample))
    out = d.detect(img, 0)
    coco = {67: "cell phone", 60: "dining table", 58: "potted plant", 57: "couch", 62: "tv", 41: "cup", 39: "bottle",
            65: "remote", 73: "book", 63: "laptop", 56: "chair", 59: "bed", 24: "backpack", 26: "handbag",
            74: "clock", 75: "vase", 61: "toilet"}
    assert {cid: d._model.names[cid] for cid in CLASSES} == coco   # our id table matches this checkpoint
    assert all(o["label"] in set(CLASSES.values()) and o["label"] != "person" for o in out)
    assert all(0 <= o["box"][0] < o["box"][2] <= img.shape[1] and 0 <= o["box"][1] < o["box"][3] <= img.shape[0] for o in out)
    room = objects.REPO_ROOT / ".cache" / "yolo" / "robotcam_sample.jpg"   # ignored local render with furniture in it
    if room.is_file():
        first = d.detect(cv2.imread(str(room)), 1000)
        assert "chair" in [o["label"] for o in first]
        again = d.detect(room.read_bytes(), 1100)                          # JPEG bytes work too; same scene keeps its ids
        assert {o["object_id"]: o["hits"] for o in again if o["label"] == "chair"} == {
            o["object_id"]: 2 for o in first if o["label"] == "chair"}
