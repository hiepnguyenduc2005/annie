"""SpacetimeGraph: places from the robot's footprint, entity intervals, true-3D queries, Elastic documents,
JSONL replay, bounded memory. Synthetic data only - no hardware, no Elasticsearch node."""
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from robot.dog.memory.graph import ELASTIC_KEYS, SpacetimeGraph  # noqa: E402

try:  # the recorder/indexer live in robot/dog/memory after the restructure; the old paths are shims
    from robot.dog.memory.spacetime import SpacetimeRecorder  # noqa: E402
except ImportError:  # pragma: no cover
    from robot.go2_spacetime import SpacetimeRecorder  # noqa: E402
try:
    from robot.dog.memory.elastic import MAPPING  # noqa: E402
except ImportError:  # pragma: no cover
    from robot.go2_spacetime_elastic import MAPPING  # noqa: E402

ROOM_A, ROOM_B = (1.5, 1.5), (8.5, 1.5)
T0 = 1_789_880_000.0  # epoch-like host receipt times, as the live loop supplies


def _graph(**kw):
    return SpacetimeGraph(center=(5.0, 1.5), **kw)


def _walk(sink, t=T0):
    """Lawnmower over room A (0..3 x 0..3), a one-cell corridor along y = 1.25, lawnmower over room B (7..10 x 0..3).
    `sink(t, x, y, yaw)` is `graph.ingest_pose` or `recorder.record_pose`. Returns the end time."""
    def sweep(x0, t):
        for row, y in enumerate(np.arange(0.25, 3.0, 0.5)):
            xs = np.arange(x0 + 0.25, x0 + 3.0, 0.25)
            for x in (xs if row % 2 == 0 else xs[::-1]):
                sink(t, float(x), float(y), 0.0)
                t += 0.5
        return t
    t = sweep(0.0, t)
    for x in np.arange(3.25, 7.0, 0.25):
        sink(t, float(x), 1.25, 0.0)
        t += 0.5
    return sweep(7.0, t)


def _person(x, y, *, identity="Jeanine", track_id=1, posture="upright", z=0.9):
    return {"track_id": track_id, "x": x, "y": y, "z": z, "label": identity or f"person {track_id}",
            "identity": identity, "posture": posture}


def _two_room_story(g):
    """Walk both rooms, then Jeanine is seen in room A for a minute and later in room B. Returns (tA0, tA1, tB0, tB1)."""
    t = _walk(g.ingest_pose)
    ta0 = t
    for n in range(121):
        g.ingest_sighting(ta0 + n * 0.5, _person(ROOM_A[0] + 0.01 * (n % 3), ROOM_A[1]))
    ta1 = ta0 + 60.0
    tb0 = ta1 + 40.0
    for n in range(121):
        g.ingest_sighting(tb0 + n * 0.5, _person(*ROOM_B, posture="lying" if n > 60 else "upright"))
    return ta0, ta1, tb0, tb0 + 60.0


def test_two_rooms_joined_by_a_corridor_are_two_places():
    g = _graph()
    _walk(g.ingest_pose)
    places = g.places()
    assert len(places) >= 2
    a, b = g.place_of(*ROOM_A), g.place_of(*ROOM_B)
    assert a["place_relation"] == b["place_relation"] == "in"
    assert a["place"] != b["place"]
    assert {a["place"], b["place"]} <= {p["id"] for p in places}
    first = next(p for p in places if p["id"] == a["place"])
    assert first["id"] == "place-1" and first["first_visit"] == T0          # numbered by first visit
    assert first["extent"]["min"] == [0.0, 0.0] and first["area_m2"] >= 9.0   # room A plus its half of the corridor
    assert first["first_visit"] < first["last_visit"]
    # a straight walk alone is never wide enough to seed a second place
    line = _graph()
    for n in range(40):
        line.ingest_pose(T0 + n, 0.25 + 0.25 * n, 1.25, 0.0)
    assert len(line.places()) == 1
    # deterministic: the same input gives the same places
    again = _graph()
    _walk(again.ingest_pose)
    assert again.places(include_cells=True) == g.places(include_cells=True)


def test_place_numbers_survive_growth_and_names_stick():
    g = _graph()
    _walk(g.ingest_pose)
    a = g.place_of(*ROOM_A)["place"]
    assert g.label_place(a, "living room") and not g.label_place("place-99", "nowhere")
    for n in range(30):  # explore further: a new strip below room A
        g.ingest_pose(T0 + 900 + n, 0.25 + 0.1 * n, -0.25, 0.0)
    assert g.place_of(*ROOM_A) == {"place": a, "place_name": "living room", "place_relation": "in"}


def test_person_in_room_a_then_room_b_gives_two_intervals_and_time_travel():
    g = _graph()
    ta0, ta1, tb0, tb1 = _two_room_story(g)
    place_a, place_b = g.place_of(*ROOM_A)["place"], g.place_of(*ROOM_B)["place"]
    tl = g.timeline("jeanine")  # case-insensitive
    assert tl["found"] and [iv["place"] for iv in tl["intervals"]] == [place_a, place_b]
    first, second = tl["intervals"]
    assert first["start"] == ta0 and first["end"] == ta1 and first["duration_s"] == 60.0
    assert second["start"] == tb0 and second["end"] == tb1 and "lying" in second["postures"]
    assert "Jeanine was in place-" in tl["text"][0]
    mid = g.where_is("Jeanine", at=(ta1 + tb0) / 2)
    assert mid["found"] and mid["place"] == place_a and mid["t"] == ta1 and mid["age_s"] == 20.0
    now = g.where_is("Jeanine")
    assert now["place"] == place_b and now["posture"] == "lying" and now["age_s"] == 0.0
    assert g.where_is("Jeanine", at=ta0 - 1)["found"] is False
    assert g.where_is("nobody")["found"] is False
    # 242 sightings at 2 Hz were thinned to about one per 2 s, plus the posture/place change points
    stored = g.stats["sightings_stored"]
    assert 60 <= stored <= 70 and g.stats["sightings"] == 242


def test_unnamed_track_is_folded_into_the_person_once_identified():
    g = _graph()
    _walk(g.ingest_pose)
    t = T0 + 500
    g.ingest_sighting(t, _person(*ROOM_A, identity=None, track_id=7))
    g.ingest_sighting(t + 3, _person(*ROOM_A, identity="Jeanine", track_id=7))
    g.ingest_sighting(t + 6, _person(*ROOM_A, identity=None, track_id=7))   # recogniser flickers: still her track
    snap = g.snapshot()
    assert [e["key"] for e in snap["entities"]] == ["Jeanine"]
    assert snap["entities"][0]["first_seen"] == t and snap["entities"][0]["last_seen"] == t + 6
    g.ingest_sighting(t + 600, _person(*ROOM_B, identity=None, track_id=7))  # a track id reused much later is a stranger
    assert {e["key"] for e in g.snapshot()["entities"]} == {"Jeanine", "person 7#7"}


def test_nearest_obstacle_is_true_3d():
    g = _graph()
    g.ingest_pose(T0, 0.0, 0.0, 0.0)
    # voxel centres (grid origin (-3.0, -6.5, -0.5), 0.2 m cells): one voxel overhead, one at body height 1 m further on
    g.ingest_obstacles(T0 + 1, [[1.1, 0.2, 2.4], [2.1, 0.2, 0.4]])
    hit = g.nearest_obstacle(1.1, 0.2, 0.4)
    assert hit["found"] and hit["voxel"]["x"] == 2.1 and hit["voxel"]["z"] == 0.4    # a 2D search would pick the overhead one
    assert hit["distance_m"] == pytest.approx(1.0, abs=0.01)
    assert g.nearest_obstacle(1.1, 0.2, 2.4)["distance_m"] == pytest.approx(0.0, abs=0.01)
    assert g.nearest_obstacle(1.1, 0.2, 2.4)["voxel"]["z"] == 2.4
    assert g.nearest_obstacle(1.1, 0.2, 1.4, z_tol_m=0.5)["found"] is False            # nothing within 0.5 m of that height
    assert g.nearest_obstacle(1.1, 0.2, 0.4, at=T0)["found"] is False                 # not seen yet at that time
    assert g.nearest_obstacle(1.1, 0.2, 0.4, max_age_s=5.0)["found"] is True
    g.ingest_pose(T0 + 100, 0.0, 0.0, 0.0)
    assert g.nearest_obstacle(1.1, 0.2, 0.4, max_age_s=5.0)["found"] is False          # stale by then
    assert g.nearest_obstacle(float("nan"), 0, 0)["found"] is False


def test_the_robots_own_body_clears_stale_voxels_but_history_and_overhead_stay():
    g = _graph()
    g.ingest_pose(T0, 0.0, 0.0, 0.0)
    g.ingest_obstacles(T0 + 1, [[1.1, 0.2, 0.4], [1.1, 0.2, 2.4]])     # e.g. a person's legs, and a shelf overhead
    assert g.nearest_obstacle(1.1, 0.2, 0.4)["distance_m"] == pytest.approx(0.0, abs=0.01)
    g.ingest_pose(T0 + 50, 1.1, 0.2, 0.0)                              # later the dog stands exactly there
    now = g.nearest_obstacle(1.1, 0.2, 0.4)
    assert now["voxel"]["z"] == 2.4 and now["distance_m"] == pytest.approx(2.0, abs=0.01)
    assert g.nearest_obstacle(1.1, 0.2, 0.4, at=T0 + 10)["distance_m"] == pytest.approx(0.0, abs=0.01)  # history intact
    assert not g.free_ahead(0.0, 0.2, 0.0, 3.0)["blocked"] and g.free_ahead(0.0, 0.2, 0.0, 3.0, at=T0 + 10)["blocked"]
    g.ingest_obstacles(T0 + 60, [[1.1, 0.2, 0.4]])                     # LiDAR sees something there again
    assert g.nearest_obstacle(1.1, 0.2, 0.4)["distance_m"] == pytest.approx(0.0, abs=0.01)


def test_summary_reports_unnamed_people_once():
    g = _graph()
    _walk(g.ingest_pose)
    for n in range(12):  # the tracker hands out a new id every few seconds for the same stranger
        g.ingest_sighting(T0 + 500 + 5 * n, _person(*ROOM_A, identity=None, track_id=100 + n))
    lines = [x for x in g.summary()["sentences"] if "unidentified" in x]
    assert len(lines) == 1 and lines[0].startswith("an unidentified person last seen just now in place-1")
    assert "12 tracker id(s)" in lines[0] and "not a head count" in lines[0]


def test_voxels_keep_first_and_last_seen_and_count_a_voxel_once_per_frame():
    g = _graph()
    g.ingest_pose(T0, 0.0, 0.0, 0.0)
    assert g.ingest_obstacles(T0 + 1, [[1.11, 0.11, 0.31], [1.12, 0.12, 0.32], [99.0, 0.0, 0.3], [np.nan, 0, 0]])
    assert g.ingest_obstacles(T0 + 9, np.array([[1.1, 0.1, 0.3]], dtype=np.float32))
    assert not g.ingest_obstacles(T0 + 10, [[1.0, 2.0]]) and not g.ingest_obstacles(None, [[1, 2, 3]])
    vox = g.voxels()
    assert vox["n"] == 1 and vox["points"][0][3:] == [2, T0 + 1, T0 + 9]
    assert g.stats["points_out_of_grid"] == 1
    assert g.voxels(at=T0)["n"] == 0 and g.voxels(min_hits=3)["n"] == 0


def test_what_is_near_uses_a_3d_radius():
    g = _graph()
    _walk(g.ingest_pose)
    t = T0 + 500
    g.ingest_sighting(t, _person(1.0, 1.0, z=0.9))
    g.ingest_sighting(t, {"track_id": 40, "x": 1.0, "y": 1.0, "z": 2.5, "label": "lamp", "kind": "object"})
    g.ingest_event(t, 1.2, 1.0, "greet", "hello")
    block = [[x, y, z] for x in (1.1, 1.3) for y in (1.0, 1.2) for z in (0.4, 0.6)]   # eight voxel centres
    g.ingest_obstacles(t, block)
    near = g.what_is_near(1.0, 1.0, 1.0, 0.4)
    assert [e["name"] for e in near["entities"]] == ["Jeanine"] and near["entities"][0]["distance_m"] == 0.1
    assert near["events"] == []                                                       # the event is 1.02 m away in 3D
    wide = g.what_is_near(1.0, 1.0, 1.0, 2.0)
    assert [e["name"] for e in wide["entities"]] == ["Jeanine", "lamp"]
    assert [e["kind"] for e in wide["events"]] == ["greet"] and wide["events"][0]["place"] == wide["place"]
    assert wide["obstacles"]["occupied_voxels"] == 8 and 0 < wide["obstacles"]["density"] < 1
    assert wide["obstacles"]["nearest_m"] == pytest.approx(0.412, abs=0.01)           # (1.1, 1.0, 0.6): hypot(0.1, 0.4)
    assert near["obstacles"]["occupied_voxels"] == 0
    assert g.what_is_near(1.0, 1.0, 1.0, 2.0, at=t - 1)["entities"] == []              # nothing had been seen yet
    assert "error" in g.what_is_near(1.0, 1.0, 1.0, 0)


def test_free_ahead_stops_at_a_remembered_wall():
    g = _graph()
    _walk(g.ingest_pose)
    g.ingest_obstacles(T0 + 500, [[2.1, y, z] for y in np.arange(0.1, 3.0, 0.2) for z in (0.3, 0.5)])
    ahead = g.free_ahead(0.5, 1.3, 0.0, 4.0)
    assert ahead["blocked"] and ahead["free_m"] == pytest.approx(1.5, abs=0.11) and ahead["hit"]["x"] == 2.1
    assert 0 < ahead["visited_m"] <= ahead["free_m"]
    back = g.free_ahead(0.5, 1.3, np.pi, 4.0)
    assert not back["blocked"] and back["free_m"] == 4.0
    assert not g.free_ahead(0.5, 1.3, 0.0, 4.0, z_band=(1.0, 2.0))["blocked"]          # nothing at that height
    assert "error" in g.free_ahead(0.5, 1.3, 0.0, -1)


def test_summary_names_the_person_a_place_and_stale_places():
    g = _graph()
    _, _, _, tb1 = _two_room_story(g)
    g.ingest_sighting(tb1 - 700, {"track_id": 3, "x": 1.0, "y": 2.0, "z": 0.4, "label": "couch", "kind": "object"})
    g.ingest_sighting(tb1 - 650, {"track_id": 4, "x": 1.4, "y": 2.2, "z": 0.5, "label": "cell phone", "kind": "object"})
    g.label_place(g.place_of(*ROOM_B)["place"], "bedroom")
    s = g.summary(tb1 + 180, limit=8)
    assert len(s["sentences"]) <= 8 and s["text"].endswith(".")
    jeanine = next(x for x in s["sentences"] if x.startswith("Jeanine"))
    assert "3 min ago" in jeanine and "in bedroom (place-" in jeanine and jeanine.endswith("lying")
    phone = next(x for x in s["sentences"] if x.startswith("cell phone"))
    assert "near the couch" in phone and "in place-1" in phone and "min ago" in phone
    assert any("not visited since" in x for x in s["sentences"])
    assert any("place(s) explored" in x for x in s["sentences"])
    assert len(g.summary(tb1, limit=2)["sentences"]) <= 2
    assert SpacetimeGraph().summary()["sentences"] == []


def test_elastic_documents_match_the_strict_mapping():
    g = _graph(run_id="run-test")
    ta0, _, _, tb1 = _two_room_story(g)
    g.ingest_sighting(tb1, {"track_id": "abc", "x": 1.0, "y": 2.0, "z": 0.4, "label": "couch", "kind": "object"})
    g.ingest_event(tb1, 8.0, 1.0, "checkin", "are you okay?")
    docs = g.to_elastic_documents()
    allowed = set(MAPPING["mappings"]["properties"])
    assert set(ELASTIC_KEYS) == allowed
    assert docs and all(set(d) == allowed for d in docs)
    json.dumps(docs, allow_nan=False)
    assert all(d["track_id"] is None or isinstance(d["track_id"], int) for d in docs)  # the mapping says integer
    assert all(d["run_id"] == "run-test" and d["@timestamp"].endswith("+00:00") for d in docs)
    person = [d for d in docs if d["kind"] == "person"]
    assert person and all(d["identity"] == "Jeanine" and "place-" in d["text"] for d in person)
    assert any("lying down" in d["text"] for d in person)
    event = next(d for d in docs if d["kind"] == "checkin")
    assert event["text"].startswith("are you okay? in ") and event["z"] == 0.0 and event["identity"] is None
    assert [d["kind"] for d in docs if d["label"] == "couch"] == ["seen"]
    assert [d["@timestamp"] for d in docs] == sorted(d["@timestamp"] for d in docs)
    later = g.to_elastic_documents(since=tb1 - 0.25)
    assert 0 < len(later) < len(docs) and g.to_elastic_documents(since=tb1) == []


def _record_story(rec):
    t = _walk(rec.record_pose)
    for n in range(40):
        rec.record_people(t + n * 0.5, [_person(*ROOM_A)])
        rec.record_obstacles(t + n * 0.5, [[2.1, 1.1, 0.3], [2.1, 1.3, 0.5]])
    for n in range(40):
        rec.record_people(t + 60 + n * 0.5, [_person(*ROOM_B, posture="lying")])
    rec.record_event(t + 80, 8.0, 1.0, "checkin", "are you okay?")
    return t


def test_replay_from_recorder_jsonl_round_trips(tmp_path):
    path = tmp_path / "run.jsonl"
    rec = SpacetimeRecorder(path=path)
    t = _record_story(rec)
    rec.close()
    live = SpacetimeGraph.from_recorder(rec, center=(5.0, 1.5))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json\n" + json.dumps({"type": "mystery", "t": 1}) + "\n")
    replay = SpacetimeGraph.from_jsonl(path, center=(5.0, 1.5))
    assert replay.skipped_lines == 2
    assert replay.places() == live.places() and len(replay.places()) >= 2
    assert replay.where_is("Jeanine") == live.where_is("Jeanine")
    assert replay.timeline("Jeanine") == live.timeline("Jeanine")
    assert [iv["place"] for iv in replay.timeline("Jeanine")["intervals"]] == \
        [replay.place_of(*ROOM_A)["place"], replay.place_of(*ROOM_B)["place"]]
    assert replay.where_is("Jeanine", at=t + 40)["place"] == replay.place_of(*ROOM_A)["place"]
    assert replay.voxels() == live.voxels() and replay.voxels()["n"] == 2
    assert replay.snapshot()["events"][0]["kind"] == "checkin" and replay.snapshot()["events"][0]["place"]
    assert replay.to_elastic_documents() == live.to_elastic_documents()


def test_ingest_all_is_idempotent_for_overlapping_snapshots_and_takes_latest():
    rec = SpacetimeRecorder()
    _record_story(rec)
    big = 10 ** 9
    snap = rec.snapshot(max_frames_out=big, max_poses_out=big, max_people_out=big)
    g = _graph()
    first = g.ingest_all(snap)
    assert first["pose"] == len(snap["poses"]) and first["people"] == len(snap["people"]) and first["event"] == 1
    before = (dict(g.stats), g.snapshot())
    assert g.ingest_all(snap) == {"pose": 0, "obstacles": 0, "people": 0, "event": 0}
    assert (dict(g.stats), g.snapshot()) == before
    t = snap["t1"] + 1.0
    rec.record_pose(t, 8.0, 1.0, 0.0)
    rec.record_people(t, [_person(8.2, 1.2)])
    assert g.ingest_all(rec.latest()) == {"pose": 1, "obstacles": 0, "people": 1, "event": 0}
    assert g.where_is("Jeanine")["x"] == 8.2
    assert g.ingest_all(None) == {"pose": 0, "obstacles": 0, "people": 0, "event": 0}


def test_memory_is_bounded_and_snapshot_is_json():
    g = _graph(max_entities=50, max_sightings=64, max_events=30)
    _walk(g.ingest_pose)
    grid_bytes = g._hits.nbytes + g._first.nbytes + g._last.nbytes
    rng = np.random.default_rng(0)
    g.ingest_sighting(T0 + 400, _person(*ROOM_A))                        # named, and the OLDEST: must survive the cap
    for n in range(700):
        g.ingest_sighting(T0 + 500 + n, _person(*ROOM_B, identity=None, track_id=1000 + n))
    for n in range(5000):                                               # one chatty track: posture flips defeat thinning
        g.ingest_sighting(T0 + 2000 + n * 0.1, _person(*ROOM_A, identity="Henry", track_id=5, posture=("upright", "sitting")[n % 2]))
    for n in range(200):
        g.ingest_event(T0 + 3000 + n, 1.0, 1.0, "collision", f"bump {n}")
        g.ingest_obstacles(T0 + 3000 + n, rng.random((400, 3)) * [40.0, 40.0, 3.0] - [15.0, 15.0, 0.5])
    snap = g.snapshot()
    text = json.dumps(snap, allow_nan=False)
    assert set(snap) >= {"places", "entities", "events", "voxels"} and snap["voxels"]["cell_m"] == 0.2
    assert snap["counts"]["entities"] == 50 and snap["counts"]["entities_dropped"] == 652
    assert {"Jeanine", "Henry"} <= {e["key"] for e in snap["entities"]}
    assert all(len(e["track"]) <= 60 and len(e["intervals"]) <= 20 for e in snap["entities"])
    assert len(g._entities["Henry"].sightings) <= 64 and len(snap["events"]) == 30
    assert g._hits.nbytes + g._first.nbytes + g._last.nbytes == grid_bytes and g.stats["points_out_of_grid"] > 0
    assert 0 < snap["voxels"]["n"] <= g._hits.size and "points" not in snap["voxels"]
    assert len(text) < 400_000
    capped = g.snapshot(max_voxels=500)["voxels"]
    assert capped["returned"] == 500 and len(capped["points"]) == 500 and capped["n"] == snap["voxels"]["n"]
    json.dumps(g.voxels(max_points=10), allow_nan=False)


def test_bad_input_is_rejected_not_raised():
    g = _graph()
    assert not g.ingest_pose("x", 0, 0, 0) and not g.ingest_pose(1.0, float("inf"), 0, 0)
    assert not g.ingest_sighting(1.0, {"x": None, "y": 2}) and not g.ingest_sighting(1.0, "nope")
    assert not g.ingest_event(None, 0, 0, "greet", "hi")
    assert g.ingest_event(5.0, None, None, None, None) and not g.ingest_event(5.0, None, None, None, None)  # retry
    assert g.places() == [] and g.snapshot()["voxels"]["n"] == 0
    assert g.nearest_obstacle(0, 0, 0)["found"] is False
    assert g.free_ahead(0, 0, 0, 2.0)["free_m"] == 2.0
    assert g.stats["rejected"] == 5
