"""PersonReid: one name per person across tracker-id churn and restarts. Synthetic frames and a fake face index:
no InsightFace, no model download."""
import json
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from robot.dog.perception.people import PeopleDirectory  # noqa: E402
from robot.dog.perception.reid import ClothingSignature, PersonReid, spoken_name  # noqa: E402

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

RED, BLUE, GREEN, DENIM, KHAKI = (0, 0, 220), (220, 0, 0), (0, 200, 0), (120, 60, 30), (140, 180, 200)
BOX_A, BOX_B = (100, 40, 180, 300), (300, 40, 380, 300)
T0 = 1_800_000_000_000  # ms; any wall-clock-like origin


def paint(img, shirt, box, legs=DENIM):
    x1, y1, x2, y2 = box
    h = y2 - y1
    img[y1 + int(0.15 * h): y1 + int(0.55 * h), x1 + 12: x2 - 12] = shirt
    img[y1 + int(0.58 * h): y1 + int(0.92 * h), x1 + 16: x2 - 16] = legs
    return img


def frame(*people):
    """A light grey room with (shirt, box[, legs]) people painted in."""
    img = np.full((360, 480, 3), (200, 200, 200), dtype=np.uint8)
    for person in people:
        paint(img, *person)
    return img


def track(track_id, box=BOX_A, identity=None):
    t = {"track_id": track_id, "box": list(box), "keypoints": [], "kp_conf": [], "conf": 0.9}
    if identity is not None:
        t["identity"] = identity
    return t


class FakeIndex:
    """Stands in for face_id.FaceIndex: names whoever's box starts at a known x; counts lookups."""

    def __init__(self, by_x=None):
        self.by_x, self.calls, self.fail = dict(by_x or {}), [], False

    def names(self):
        return sorted(set(self.by_x.values()))

    def identify(self, jpeg, box):
        self.calls.append(box)
        if self.fail:
            raise RuntimeError("model exploded")
        assert isinstance(jpeg, bytes)
        name = self.by_x.get(int(box[0])) if box is not None else None
        return {"name": name, "score": 0.71} if name else None

    def forget(self, name):
        self.by_x = {x: n for x, n in self.by_x.items() if n != name}

    def save(self, path):
        pass


def directory(tmp_path, index=None):
    return PeopleDirectory(tmp_path / "faces", index_factory=(lambda: index) if index is not None else (lambda: None))


def jpeg_of(img):
    return cv2.imencode(".jpg", img)[1].tobytes()


def see(reid, img, tracks, t_ms, **kw):
    return reid.apply(img, jpeg_of(img), tracks, t_ms, **kw)


def make_guest(reid, shirt, *, track_id=1, start_ms=T0, legs=DENIM):
    img = frame((shirt, BOX_A, legs))
    out = None
    for k in range(3):
        out = see(reid, img, [track(track_id)], start_ms + 1000 * k)[0]["identity"]
    return out


# -- the signature -----------------------------------------------------------------------------------------------
def test_signature_separates_red_from_blue_and_tolerates_light_and_position():
    red = ClothingSignature.from_track(frame((RED, BOX_A)), track(1))
    blue = ClothingSignature.from_track(frame((BLUE, BOX_A)), track(1))
    dim_red_elsewhere = ClothingSignature.from_track(frame(((0, 0, 170), BOX_B)), track(2, BOX_B))
    assert ClothingSignature.distance(red, red) == 0.0
    assert ClothingSignature.distance(red, blue) > 0.5
    assert ClothingSignature.distance(red, dim_red_elsewhere) < 0.15
    assert 0.0 <= ClothingSignature.distance(red, blue) <= 1.0
    assert len(red.hist) == 32 and abs(sum(red.hist) - 1.0) < 1e-3


def test_signature_tells_black_from_white_and_survives_missing_keypoints_and_legs():
    black = ClothingSignature.from_track(frame(((20, 20, 20), BOX_A)), track(1))
    white = ClothingSignature.from_track(frame(((245, 245, 245), BOX_A)), track(1))
    assert ClothingSignature.distance(black, white) > 0.4  # no hue to go on: brightness bins carry it
    partial = track(1)
    partial["keypoints"] = [[0, 0]] * 5 + [[112, 80], [168, 80]] + [[0, 0]] * 10  # shoulders only
    partial["kp_conf"] = [0.0] * 5 + [0.9, 0.9] + [0.0] * 10
    a = ClothingSignature.from_track(frame((RED, BOX_A)), partial)
    assert a is not None and a.ratio is None
    assert ClothingSignature.distance(a, ClothingSignature.from_track(frame((RED, BOX_A)), track(1))) < 0.05
    cut_off = track(1, (100, 200, 180, 700))  # legs below the frame
    no_legs = ClothingSignature.from_track(paint(np.full((360, 480, 3), 200, np.uint8), RED, (100, 200, 180, 360)), cut_off)
    assert no_legs is not None  # distance still defined when one side has no lower body
    assert 0.0 <= ClothingSignature.distance(no_legs, a) <= 1.0
    assert ClothingSignature.from_track(frame(), track(1, (900, 900, 980, 990))) is None  # box outside the frame


def test_signature_is_cheap():
    img, t = frame((RED, BOX_A)), track(1)
    ClothingSignature.from_track(img, t)
    start = time.perf_counter()
    for _ in range(200):
        ClothingSignature.from_track(img, t)
    assert (time.perf_counter() - start) / 200 < 0.002


def test_signature_json_round_trip_and_rejects_damage():
    sig = ClothingSignature.from_track(frame((RED, BOX_A)), track(1))
    again = ClothingSignature.from_json(json.loads(json.dumps(sig.to_json())))
    assert ClothingSignature.distance(sig, again) == 0.0
    assert ClothingSignature.from_json({"hist": [1.0], "upper": [0, 0, 0]}) is None
    assert ClothingSignature.from_json({"hist": ["x"] * 32, "upper": [0, 0, 0]}) is None


# -- face, then clothing as the bridge ---------------------------------------------------------------------------
def test_face_names_the_track_and_same_clothes_on_a_new_track_id_keep_the_name(tmp_path):
    reid = PersonReid(directory(tmp_path, FakeIndex({BOX_A[0]: "Tom"})))
    img = frame((GREEN, BOX_A))
    first = see(reid, img, [track(1)], T0)[0]["identity"]
    assert first == {"name": "Tom", "score": 0.71, "method": "face"}
    # he turned round and the tracker lost him: new id, other side of the frame, no face to be found
    later = see(reid, frame((GREEN, BOX_B)), [track(7, BOX_B)], T0 + 4000)[0]["identity"]
    assert later["name"] == "Tom" and later["method"] == "clothing" and later["score"] > 0.8


def test_different_clothes_do_not_inherit_the_name(tmp_path):
    reid = PersonReid(directory(tmp_path, FakeIndex({BOX_A[0]: "Tom"})))
    see(reid, frame((GREEN, BOX_A)), [track(1)], T0)
    stranger = see(reid, frame((BLUE, BOX_B, KHAKI)), [track(8, BOX_B)], T0 + 4000)[0]
    assert stranger["identity"] is None


def test_face_match_beats_clothing(tmp_path):
    index = FakeIndex({BOX_A[0]: "Tom"})
    reid = PersonReid(directory(tmp_path, index))
    see(reid, frame((GREEN, BOX_A)), [track(1)], T0)  # Tom is known in green
    index.by_x = {BOX_B[0]: "Ana"}  # Ana walks in wearing the same green top; her face is visible
    ana = see(reid, frame((GREEN, BOX_B)), [track(2, BOX_B)], T0 + 5000)[0]["identity"]
    assert ana == {"name": "Ana", "score": 0.71, "method": "face"}


def test_tracker_supplied_face_identity_wins_and_no_second_lookup_is_made(tmp_path):
    index = FakeIndex({BOX_A[0]: "Someone Else"})
    reid = PersonReid(directory(tmp_path, index))
    out = see(reid, frame((GREEN, BOX_A)), [track(1, identity={"name": "Tom", "score": 0.66})], T0)[0]["identity"]
    assert out == {"name": "Tom", "score": 0.66, "method": "face"} and index.calls == []


def test_face_lookups_are_rate_limited_per_track_and_failures_are_swallowed(tmp_path):
    index = FakeIndex({})  # nobody matches
    index.by_x = {999: "Tom"}
    reid = PersonReid(directory(tmp_path, index), face_every_ms=1500, guest_min_sightings=99)
    img = frame((GREEN, BOX_A))
    for k in range(10):  # 10 frames in 900 ms
        see(reid, img, [track(1)], T0 + 100 * k)
    assert len(index.calls) == 1
    see(reid, img, [track(1)], T0 + 1600)
    assert len(index.calls) == 2
    for k in range(10):  # the tracker invents a new id every frame: still not a lookup per frame
        see(reid, img, [track(100 + k)], T0 + 2200 + 50 * k)
    assert len(index.calls) == 3
    index.fail = True
    out = see(reid, img, [track(1)], T0 + 3200)
    assert out[0]["identity"] is None and reid.face_error == "RuntimeError"


def test_live_path_without_a_jpeg_encodes_the_head_crop_itself(tmp_path):
    seen = []

    class CropIndex(FakeIndex):
        def identify(self, jpeg, box):
            seen.append((cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape, box))
            return {"name": "Tom", "score": 0.8}

    reid = PersonReid(directory(tmp_path, CropIndex({1: "Tom"})))
    img = frame((GREEN, BOX_A))
    tracks = [track(1)]
    assert reid.apply(img, tracks) is tracks  # the pipeline's two-argument call
    assert tracks[0]["identity"]["method"] == "face"
    (shape, box), = seen
    assert box is None and shape[0] == 130 and shape[1] == 96  # top half of the box, 10 % wider


def test_a_track_id_that_jumps_onto_someone_else_loses_the_face_name(tmp_path):
    index = FakeIndex({BOX_A[0]: "Tom"})
    reid = PersonReid(directory(tmp_path, index))
    see(reid, frame((GREEN, BOX_A)), [track(1)], T0)
    index.by_x = {}
    assert see(reid, frame((GREEN, BOX_A)), [track(1)], T0 + 100)[0]["identity"]["method"] == "face"  # held, no lookup
    swapped = see(reid, frame((BLUE, BOX_A, KHAKI)), [track(1)], T0 + 200)[0]["identity"]
    assert swapped is None


def test_one_name_names_one_person_per_frame(tmp_path):
    reid = PersonReid(directory(tmp_path, FakeIndex({BOX_A[0]: "Tom"})))
    see(reid, frame((GREEN, BOX_A)), [track(1)], T0)
    twins = see(reid, frame((GREEN, BOX_A), (GREEN, BOX_B)), [track(1), track(2, BOX_B)], T0 + 100)
    assert [t["identity"] and t["identity"]["name"] for t in twins] == ["Tom", None]


# -- guests ------------------------------------------------------------------------------------------------------
def test_guest_only_after_three_spaced_sightings_and_never_a_face(tmp_path):
    index = FakeIndex({999: "Tom"})
    reid = PersonReid(directory(tmp_path, index))
    img = frame((BLUE, BOX_A))
    assert see(reid, img, [track(1)], T0)[0]["identity"] is None
    for k in range(1, 6):  # a burst of frames is still the first sighting
        assert see(reid, img, [track(1)], T0 + 40 * k)[0]["identity"] is None
    assert see(reid, img, [track(1)], T0 + 1000)[0]["identity"] is None
    third = see(reid, img, [track(1)], T0 + 2000)[0]["identity"]
    assert third == {"name": "Guest 1", "score": 1.0, "method": "guest"}
    assert reid.guests()[0]["name"] == "Guest 1"
    saved = json.loads((tmp_path / "faces" / "guests.json").read_text())
    assert set(saved["guests"]) == {"Guest 1"} and saved["next_guest"] == 2
    record = saved["guests"]["Guest 1"]
    assert set(record) == {"sig", "source", "created_s", "last_seen_s", "sightings", "xy", "xy_s"}
    assert set(record["sig"]) == {"hist", "upper", "lower", "ratio"}  # appearance numbers only: no image, no face
    assert stat.S_IMODE((tmp_path / "faces" / "guests.json").stat().st_mode) == 0o600


def test_guest_survives_tracker_churn_and_a_restart_and_a_second_guest_is_numbered_next(tmp_path):
    people = directory(tmp_path)
    assert make_guest(PersonReid(people), BLUE)["name"] == "Guest 1"
    restarted = PersonReid(directory(tmp_path), clock=lambda: (T0 + 60_000) / 1000)
    again = see(restarted, frame((BLUE, BOX_B)), [track(42, BOX_B)], T0 + 61_000)[0]["identity"]
    assert again["name"] == "Guest 1" and again["method"] == "guest"  # first frame after the restart
    other = make_guest(restarted, (0, 140, 255), track_id=43, start_ms=T0 + 70_000, legs=KHAKI)
    assert other["name"] == "Guest 2"
    assert [g["name"] for g in restarted.guests()] == ["Guest 1", "Guest 2"]


def test_guests_expire_and_their_numbers_are_not_reused(tmp_path):
    reid = PersonReid(directory(tmp_path), forget_after_s=1800)
    make_guest(reid, BLUE)
    late = T0 + 1_900_000
    assert see(reid, frame((RED, BOX_B, KHAKI)), [track(5, BOX_B)], late)[0]["identity"] is None
    assert reid.guests() == []
    assert make_guest(reid, BLUE, track_id=6, start_ms=late + 1000)["name"] == "Guest 2"
    # and a restart after the expiry loads nobody
    reid.save()
    assert PersonReid(directory(tmp_path), clock=lambda: (late + 4_000_000) / 1000).guests() == []


def test_forgetting_a_guest_and_an_enrolled_persons_clothing(tmp_path):
    index = FakeIndex({BOX_B[0]: "Tom"})
    people = directory(tmp_path, index)
    people.enroll("Tom", [], relation="son")
    reid = people.reid()
    assert people.reid() is reid
    make_guest(reid, BLUE)
    see(reid, frame((GREEN, BOX_B)), [track(2, BOX_B)], T0 + 5000)
    listed = {p["name"]: p for p in people.list()}
    assert listed["Guest 1"]["guest"] is True and listed["Guest 1"]["faces"] == 0 and listed["Tom"]["guest"] is False
    assert people.forget("Guest 1") is True
    assert [p["name"] for p in people.list()] == ["Tom"]
    assert see(reid, frame((BLUE, BOX_A)), [track(9)], T0 + 6000)[0]["identity"] is None  # a stranger again
    assert people.forget("Tom") is True
    index.by_x = {}
    assert see(reid, frame((GREEN, BOX_B)), [track(3, BOX_B)], T0 + 7000)[0]["identity"] is None
    saved = json.loads((tmp_path / "faces" / "guests.json").read_text())
    assert saved["guests"] == {} and saved["known"] == {}
    assert people.forget("Nobody") is False


def test_a_guest_who_turns_out_to_be_family_stops_being_a_guest(tmp_path):
    index = FakeIndex({})
    index.by_x = {999: "Tom"}
    reid = PersonReid(directory(tmp_path, index))
    assert make_guest(reid, GREEN)["name"] == "Guest 1"  # back turned the whole time
    index.by_x = {BOX_A[0]: "Tom"}
    named = see(reid, frame((GREEN, BOX_A)), [track(1)], T0 + 4000)[0]["identity"]
    assert named["name"] == "Tom" and named["method"] == "face"
    assert reid.guests() == []
    assert see(reid, frame((GREEN, BOX_B)), [track(2, BOX_B)], T0 + 9000)[0]["identity"]["name"] == "Tom"


def test_damaged_guest_file_is_ignored(tmp_path):
    (tmp_path / "faces").mkdir()
    (tmp_path / "faces" / "guests.json").write_text('{"guests": {"Guest 3": {"sig": {"hist": [1]}}, "Tom": {}}, "next_guest": "x"}')
    reid = PersonReid(directory(tmp_path))
    assert reid.guests() == [] and reid.next_guest == 1


# -- the shirt rule and the place prior --------------------------------------------------------------------------
def test_shirt_colour_still_names_jeanine_with_no_face_and_no_history(tmp_path):
    people = directory(tmp_path)
    people.enroll("Jeanine", [], shirt="red")
    reid = people.reid()
    t = see(reid, frame((RED, BOX_A)), [track(1)], T0)[0]
    assert t["identity"] == {"name": "Jeanine", "score": pytest.approx(1.0, abs=0.05), "method": "shirt_colour"}
    assert t["shirt_colour_fraction"] > 0.9
    assert see(reid, frame((RED, BOX_B)), [track(2, BOX_B)], T0 + 3000)[0]["identity"]["name"] == "Jeanine"
    blue = see(reid, frame((BLUE, BOX_A)), [track(3)], T0 + 6000)[0]
    assert blue["identity"] is None


def test_shirt_rule_from_the_command_line_follows_the_live_target_and_needs_no_directory():
    class Target:  # patrol's TargetIdentifier: the /people handler renames it while the dog runs
        name, colour = "Grandma", "red"

    target = Target()
    reid = PersonReid(None, shirt_rules=lambda: [(target.name, target.colour)], path="/nonexistent/guests.json")
    assert see(reid, frame((RED, BOX_A)), [track(1)], T0)[0]["identity"]["name"] == "Grandma"
    target.name, target.colour = "Jeanine", "blue"  # what the rule taught about "Grandma" goes with the rule
    assert see(reid, frame((RED, BOX_A)), [track(2)], T0 + 1000)[0]["identity"] is None
    named = see(reid, frame((BLUE, BOX_B)), [track(3, BOX_B)], T0 + 2000)[0]["identity"]
    assert named["name"] == "Jeanine" and named["method"] == "shirt_colour"
    assert spoken_name(named) == "Jeanine"
    assert spoken_name({"name": "Guest 2", "score": 1.0, "method": "guest"}) is None and spoken_name(None) is None


def test_place_prior_relaxes_the_clothing_match_only_near_and_recently(tmp_path):
    def reid_knowing_tom(sub):
        reid = PersonReid(directory(tmp_path / sub, FakeIndex({BOX_A[0]: "Tom"})))
        see(reid, frame((GREEN, BOX_A)), [track(1)], T0, pose_xy=(2.0, 1.0))
        reid.directory.index().by_x = {999: "Tom"}
        return reid

    # the same green top with a pale bag held across 40 % of it: just outside the plain threshold
    faded = frame((GREEN, BOX_B))
    top, bottom = BOX_B[1] + int(0.15 * 260), BOX_B[1] + int(0.55 * 260)
    faded[top: top + int(0.4 * (bottom - top)), BOX_B[0] + 12: BOX_B[2] - 12] = (235, 235, 235)
    sig_known = ClothingSignature.from_track(frame((GREEN, BOX_A)), track(1))
    d = ClothingSignature.distance(sig_known, ClothingSignature.from_track(faded, track(5, BOX_B)))
    assert 0.28 < d <= 0.36, d

    assert see(reid_knowing_tom("a"), faded, [track(5, BOX_B)], T0 + 30_000, pose_xy=(2.5, 1.4))[0]["identity"]["name"] == "Tom"
    assert see(reid_knowing_tom("b"), faded, [track(5, BOX_B)], T0 + 30_000, pose_xy=(6.0, 1.0))[0]["identity"] is None
    assert see(reid_knowing_tom("c"), faded, [track(5, BOX_B)], T0 + 700_000, pose_xy=(2.0, 1.0))[0]["identity"] is None
    assert see(reid_knowing_tom("d"), faded, [track(5, BOX_B)], T0 + 30_000)[0]["identity"] is None  # no pose, no prior

    from_source = reid_knowing_tom("e")
    from_source.pose_source = lambda: (2.2, 1.1)
    tracks = [track(5, BOX_B)]
    from_source.clock = lambda: (T0 + 30_000) / 1000
    from_source.apply(faded, tracks)
    assert tracks[0]["identity"]["name"] == "Tom"


def selection_reid(tmp_path):
    return PersonReid(path=tmp_path / "guests.json", guest_min_sightings=1,
                      clock=lambda: T0 / 1000)


def test_operator_assignment_is_face_free_and_survives_turn_and_track_churn(tmp_path):
    reid = selection_reid(tmp_path)
    assert reid.selection_status() == {"name": None, "guest": None, "state": "unselected", "needs_selection": True}
    see(reid, frame((KHAKI, BOX_A, DENIM)), [track(1)], T0)
    # Neither assignment nor clothing-only tracking needs a face provider.
    class NoFaces:
        def index(self):
            raise AssertionError("No face operation allowed")
    reid.directory = NoFaces()
    result = reid.assign_guest("Guest 1")
    assert result == {"name": "Jeanine", "guest": "Guest 1", "state": "tracking", "needs_selection": False}
    result["state"] = "changed"
    assert reid.selection_status()["state"] == "tracking"
    assert reid.guests() == [] and not reid._tracks
    out = see(reid, frame((KHAKI, BOX_B, DENIM)), [track(42, BOX_B)], T0 + 1000)
    assert out[0]["identity"] == {"name": "Jeanine", "score": 1.0, "method": "clothing"}
    reid.save()
    assert "Jeanine" not in json.loads(reid.path.read_text())["known"]
    assert selection_reid(tmp_path).selection_status()["state"] == "unselected"


def test_assignment_rejects_invalid_absent_stale_and_no_longer_visible_guests(tmp_path):
    reid = selection_reid(tmp_path)
    for guest in (None, "Jeanine", "Guest 99"):
        with pytest.raises(ValueError):
            reid.assign_guest(guest)
    see(reid, frame((KHAKI, BOX_A)), [track(1)], T0)
    with pytest.raises(ValueError, match="stale"):
        reid.assign_guest("Guest 1", now_s=T0 / 1000 + 2.001)
    see(reid, frame(), [], T0 + 100)
    with pytest.raises(ValueError, match="visible"):
        reid.assign_guest("Guest 1", now_s=T0 / 1000 + .1)


def test_assignment_requires_current_lower_body_signature(tmp_path):
    reid = selection_reid(tmp_path)
    cropped = track(1, (100, 200, 180, 700))
    img = paint(np.full((360, 480, 3), 200, np.uint8), KHAKI, (100, 200, 180, 360))
    see(reid, img, [cropped], T0)
    assert reid.guests()[0]["name"] == "Guest 1"
    with pytest.raises(ValueError, match="lower"):
        reid.assign_guest("Guest 1")


def test_duplicate_clothing_invalidates_selection_and_requires_reselection(tmp_path):
    reid = selection_reid(tmp_path)
    see(reid, frame((KHAKI, BOX_A)), [track(1)], T0)
    reid.assign_guest("Guest 1")
    twins = see(reid, frame((KHAKI, BOX_A), (KHAKI, BOX_B)), [track(1), track(2, BOX_B)], T0 + 100)
    assert all(t["identity"] is None or t["identity"]["name"] != "Jeanine" for t in twins)
    assert reid.selection_status()["state"] == "ambiguous"
    assert reid.selection_status()["needs_selection"] is True
    lone = see(reid, frame((KHAKI, BOX_B)), [track(3, BOX_B)], T0 + 200)[0]
    assert lone["identity"]["name"] != "Jeanine"
    assert reid.selection_status()["state"] == "ambiguous"
    reid.assign_guest(lone["identity"]["name"], now_s=T0 / 1000 + .2)
    assert reid.selection_status()["state"] == "tracking"
    assert see(reid, frame((KHAKI, BOX_B)), [track(4, BOX_B)], T0 + 300)[0]["identity"]["name"] == "Jeanine"


def test_lost_selection_is_sticky_and_clock_expiry_clears_cached_identity(tmp_path):
    reid = selection_reid(tmp_path)
    see(reid, frame((KHAKI, BOX_A)), [track(1)], T0)
    reid.assign_guest("Guest 1")
    see(reid, frame((KHAKI, BOX_A)), [track(1)], T0 + 100)
    reid.clock = lambda: T0 / 1000 + 2.101
    assert reid.selection_status()["state"] == "lost"
    assert all(t["name"] != "Jeanine" for t in reid._tracks.values())
    out = see(reid, frame((KHAKI, BOX_A)), [track(1)], T0 + 2200)[0]
    assert out["identity"]["name"] != "Jeanine"
    assert reid.selection_status()["needs_selection"] is True
    reid.assign_guest(out["identity"]["name"], now_s=T0 / 1000 + 2.2)
    assert reid.forget("Jeanine") is True
    assert reid.selection_status()["state"] == "lost"
