import json
import re
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_spacetime import SpacetimeRecorder, load_jsonl, spacetime_http  # noqa: E402

np = pytest.importorskip("numpy")

VIEWER = Path(__file__).resolve().parents[1] / "spacetime_viewer.html"


def _cloud(n, offset=0.0):
    rng = np.random.default_rng(1)
    return (rng.random((n, 3)) * 4.0 + offset).astype(np.float32)


def _person(track_id=1, x=1.0, y=2.0, **extra):
    return {"track_id": track_id, "x": x, "y": y, "z": 0.9, "label": "person", "identity": None, "posture": "standing", **extra}


def test_obstacles_are_subsampled_rounded_and_cleaned():
    rec = SpacetimeRecorder(max_points_per_frame=50)
    pts = _cloud(1000)
    pts[0] = [np.nan, 0.0, 0.0]
    assert rec.record_obstacles(1.0, pts)
    assert rec.record_obstacles(2.0, _cloud(20))  # below the cap: kept whole
    frames = rec.snapshot()["frames"]
    assert [len(f["points"]) for f in frames] == [50, 20]
    flat = [v for f in frames for p in f["points"] for v in p]
    assert all(v == round(v, 2) for v in flat)
    source = {tuple(p) for p in np.round(pts[1:].astype(np.float64), 2).tolist()}
    assert all(tuple(p) in source for p in frames[0]["points"])  # a subset of the input, nothing invented
    assert not rec.record_obstacles(3.0, np.zeros((0, 3)))
    assert not rec.record_obstacles(3.0, [[np.inf, 0, 0]])
    assert not rec.record_obstacles(float("nan"), _cloud(5))
    assert not rec.record_obstacles(3.0, [1.0, 2.0, 3.0])  # wrong shape


def test_input_rate_limits_thin_high_rate_streams():
    rec = SpacetimeRecorder(max_seconds=900.0, max_frames=1800)  # default frame spacing 0.5 s
    stored = [rec.record_obstacles(i * 0.1, _cloud(10)) for i in range(11)]
    assert stored.count(True) == 3  # t = 0.0, 0.5, 1.0
    assert [rec.record_pose(i * 0.05, i, 0.0, 0.0) for i in range(9)].count(True) == 3  # pose_min_dt 0.2: t = 0, .2, .4
    assert rec.latest()["pose"] == [0.4, 8.0, 0.0, 0.0]  # latest() still has the freshest pose
    assert rec.record_people(0.0, [_person()])
    assert not rec.record_people(0.1, [_person()])  # same track, same state, too soon
    assert rec.record_people(0.15, [_person(posture="lying")])  # a posture change is never thinned
    assert rec.record_people(0.16, [_person(track_id=2)])  # another track
    assert [p["posture"] for p in rec.snapshot()["people"]] == ["standing", "lying", "standing"]


def test_snapshot_since_until_filtering():
    rec = SpacetimeRecorder(min_frame_dt=0.0, pose_min_dt=0.0, people_min_dt=0.0)
    for t in range(10):
        rec.record_pose(t, t * 0.1, 0.0, 0.0)
        rec.record_obstacles(t, _cloud(5))
        rec.record_people(t, [_person(x=t)])
    rec.record_event(4.0, 0.4, 0.0, "greet", "hello Henry")
    snap = rec.snapshot(since=3, until=6)
    assert [p[0] for p in snap["poses"]] == [4.0, 5.0, 6.0]  # since exclusive, until inclusive
    assert [f["t"] for f in snap["frames"]] == [4.0, 5.0, 6.0]
    assert [p["t"] for p in snap["people"]] == [4.0, 5.0, 6.0]
    assert [e["kind"] for e in snap["events"]] == ["greet"]
    assert (snap["t0"], snap["t1"], snap["span"]) == (4.0, 6.0, [0.0, 9.0])
    tail = rec.snapshot(since=snap["t1"])  # the page's live poll: no duplicates
    assert [f["t"] for f in tail["frames"]] == [7.0, 8.0, 9.0]
    empty = rec.snapshot(since=9.0)
    assert empty["t0"] is None and empty["t1"] is None and empty["frames"] == [] and empty["span"] == [0.0, 9.0]


def test_memory_bound_drops_old_and_excess_frames():
    rec = SpacetimeRecorder(max_seconds=10.0, max_frames=5, min_frame_dt=0.0, pose_min_dt=0.0, people_min_dt=0.0)
    for t in range(8):
        rec.record_obstacles(float(t), _cloud(3))
    assert [f["t"] for f in rec.snapshot()["frames"]] == [3.0, 4.0, 5.0, 6.0, 7.0]  # max_frames
    rec.record_pose(1.0, 0.0, 0.0, 0.0)
    rec.record_people(1.0, [_person()])
    rec.record_event(1.0, 0.0, 0.0, "collision", "bump")
    rec.record_pose(16.5, 1.0, 0.0, 0.0)  # everything older than 6.5 s falls out of max_seconds
    snap = rec.snapshot()
    assert [f["t"] for f in snap["frames"]] == [7.0]
    assert [p[0] for p in snap["poses"]] == [16.5]
    assert snap["people"] == [] and snap["events"] == []
    assert snap["span"] == [7.0, 16.5]


def test_jsonl_round_trip(tmp_path):
    path = tmp_path / "runs" / "patrol.jsonl"
    rec = SpacetimeRecorder(path=path, max_points_per_frame=30)
    for i in range(6):
        t = 100.0 + i
        rec.record_pose(t, i * 0.25, -i * 0.1, 0.1 * i)
        rec.record_obstacles(t, _cloud(200, offset=i))
        rec.record_people(t, [_person(track_id=np.int64(7), x=1.0 + i, identity="Henry", posture="lying")])
    rec.record_event(103.0, None, None, "checkin", "are you OK?")  # no position: uses the freshest pose
    assert not rec.record_event(103.0, None, None, "checkin", "are you OK?")  # a retry is not a second event
    rec.close()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert {d["type"] for d in lines} == {"pose", "obstacles", "people", "event"} and len(lines) == 19
    with path.open("a") as fh:
        fh.write("not json\n{\"type\": \"pose\"}\n")
    replay = load_jsonl(path)
    assert replay.skipped_lines == 2 and replay.path is None
    assert replay.snapshot() == rec.snapshot()
    assert replay.snapshot()["people"][0]["track_id"] == 7
    assert replay.snapshot()["events"][0]["x"] == 1.25  # pose at t=105 was the freshest when the event arrived


def test_snapshot_is_json_and_evenly_subsampled():
    rec = SpacetimeRecorder(min_frame_dt=0.0)
    for i in range(1000):
        rec.record_obstacles(i * 0.5, _cloud(4))
    snap = rec.snapshot()
    times = [f["t"] for f in snap["frames"]]
    assert len(times) == 300 and times[0] == 0.0 and times[-1] == 499.5
    gaps = np.diff(times)
    assert gaps.min() >= 1.5 and gaps.max() <= 2.0  # evenly spread, not just the newest 300
    assert snap["counts"]["frames_total"] == 1000 and snap["counts"]["frames"] == 300
    assert len(rec.snapshot(max_frames_out=7)["frames"]) == 7
    assert len(rec.snapshot(since=400.0)["frames"]) == 199  # a short range is returned whole
    assert json.loads(json.dumps(snap, allow_nan=False)) == snap


def test_full_run_snapshot_stays_small():
    rec = SpacetimeRecorder()
    cloud = _cloud(5000, offset=-10.0)
    for i in range(1800):
        t = 1_700_000_000.0 + i * 0.5
        rec.record_obstacles(t, cloud)
        rec.record_pose(t, i * 0.01, 0.0, 0.0)
        rec.record_people(t, [_person(track_id=k, x=k, identity="Somebody Long Name") for k in range(3)])
    size = len(json.dumps(rec.snapshot(), separators=(",", ":")))
    assert size < 8_000_000


def test_latest_shape():
    rec = SpacetimeRecorder()
    assert rec.latest() == {"t": None, "pose": None, "people_t": None, "people": [], "frame": None, "events": []}
    rec.record_pose(5.0, 1.0, 2.0, 0.5)
    rec.record_obstacles(5.0, _cloud(10))
    rec.record_people(5.0, [_person(), {"track_id": 9, "x": "bad", "y": 0.0}, "junk"])
    rec.record_people(6.0, [])  # nobody in view any more
    rec.record_event(6.0, 1.0, 2.0, "voice", "come here")
    latest = rec.latest()
    assert latest["pose"] == [5.0, 1.0, 2.0, 0.5] and latest["t"] == 6.0
    assert latest["people"] == [] and latest["people_t"] == 6.0
    assert latest["frame"]["t"] == 5.0 and len(latest["frame"]["points"]) == 10
    assert latest["events"][0]["kind"] == "voice"
    assert len(rec.snapshot()["people"]) == 1  # the malformed people entries were dropped
    json.dumps(latest, allow_nan=False)


def test_concurrent_record_and_snapshot():
    rec = SpacetimeRecorder(min_frame_dt=0.0, pose_min_dt=0.0, max_frames=50)
    errors = []

    def writer(k):
        try:
            for i in range(200):
                rec.record_obstacles(k * 1000 + i, _cloud(20))
                rec.record_pose(k * 1000 + i, i, k, 0.0)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(3)]
    for th in threads:
        th.start()
    for _ in range(20):
        json.dumps(rec.snapshot())
    for th in threads:
        th.join()
    assert not errors and len(rec.snapshot()["frames"]) <= 50


def test_http_routes():
    rec = SpacetimeRecorder(min_frame_dt=0.0)
    for t in range(5):
        rec.record_obstacles(t, _cloud(3))
    assert spacetime_http(rec, "/status.json") is None  # not ours: the live view keeps handling it
    code, body, ctype = spacetime_http(rec, "/spacetime.json?since=2")
    assert code == 200 and ctype == "application/json" and [f["t"] for f in json.loads(body)["frames"]] == [3.0, 4.0]
    assert len(json.loads(spacetime_http(rec, "/spacetime.json?max_frames=2")[1])["frames"]) == 2
    assert spacetime_http(rec, "/spacetime.json?since=nan")[0] == 400
    assert spacetime_http(rec, "/spacetime.json?since=abc")[0] == 400
    assert spacetime_http(None, "/spacetime.json")[0] == 503
    assert json.loads(spacetime_http(rec, "/spacetime_latest.json")[1])["frame"]["t"] == 4.0
    code, body, ctype = spacetime_http(rec, "/spacetime")
    assert code == 200 and ctype.startswith("text/html") and b"/spacetime.json" in body


def test_viewer_page_is_self_contained():
    html = VIEWER.read_text(encoding="utf-8")
    assert "/spacetime.json" in html and "three.min.js" in html
    assert "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js" in html
    hosts = set(re.findall(r"(?:https?|wss?):/+([^/\"'\s)]+)", html))
    hosts |= set(re.findall(r"""(?:src|href|url)\s*[=(]\s*["']?//([^/"'\s)]+)""", html))  # protocol-relative
    assert hosts == {"cdnjs.cloudflare.com"} or hosts <= {"cdnjs.cloudflare.com", "cdn.jsdelivr.net"}, hosts
