# Intelligent Dog Brain, step 1-3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the simulated/physical dog explicit search progress, event-driven re-planning that survives the 5 s gate locally, an independent posture signal for the fall trigger, and goal-conditioned semantic memory.

**Architecture:** Pure, deterministic policy modules (`mission.py`, `posture.py`, `semantic_memory.py`) are added beside the existing services and wired in through additive, optional contract fields. The bridge keeps perception continuous and plans only on events; the brain service packs mission state into the planner context and never discards a perception when the action model fails.

**Tech Stack:** Python 3.12, pydantic StrictModel contracts, httpx, pytest, Ultralytics YOLO11 (detect + pose, ByteTrack), Ollama (`qwen3:8b`, `nomic-embed-text`), SQLite.

**Spec:** `docs/superpowers/specs/2026-09-19-intelligent-dog-brain-design.md`

## Global Constraints

- Contracts in `robot/contract/brain.md` and `robot/contract/README.md` stay backward compatible: new request fields are optional; `StrictModel` (`extra='forbid', strict=True`) stays.
- Capture identity (`frame_id`, `ts`, `pose{x,y,yaw,map_id}`) is never re-dated or synthesized. No synthetic observations.
- The 5 s freshness gate (`STALE_MS`, `gate_action`) is unchanged.
- Incident ownership stays in `app_backend`; the bridge sends only `goto|look|say|stop`.
- Tests are offline: no Ollama, no network, no hardware. Mock providers with injected callables.
- Run from the repo root with `.venv/bin/python -m pytest <path> -q`. Do not touch `robot/simulation/viewer.py`, `house_demo.py`, `run_brain.py`, `agent_execution.py` (a teammate's uncommitted work) except where a task names them.
- Commit only the files a task names. Never `git add -A`.

---

### Task 1: MissionState (pure search-progress model)

**Files:**
- Create: `robot/simulation/mission.py`
- Test: `robot/simulation/tests/test_mission.py`

**Interfaces:**
- Consumes: perception dicts as ingested today (`person: bool, posture: str, location: str, confidence: float, frame_id: str, ts: int, pose: {x,y,yaw,map_id}`), waypoint dicts `{id, x, y}`, command outcome dicts `{cmd, waypoint?, status, command_id?}`.
- Produces:
  ```python
  class MissionState:
      def __init__(self, map_id: str, waypoints: list[dict], *, inspected_ttl_ms: int = 120_000, arrive_radius_m: float = 1.0)
      def observe(self, perception: dict, *, now_ms: int) -> list[str]   # events: 'person_seen' | 'inspected_empty' | []
      def command_update(self, outcome: dict, *, now_ms: int) -> list[str]  # events: 'arrived' | 'command_failed' | []
      def next_search_target(self, pose: dict, *, now_ms: int) -> str | None
      def summary(self, *, now_ms: int) -> dict
      # summary shape:
      # {'waypoints': [{'id': str, 'status': 'unvisited'|'visited'|'inspected_empty'|'person_seen', 'age_s': int|None}],
      #  'last_sighting': {'frame_id','ts','pose','waypoint_id','posture','location','confidence'} | None,
      #  'destination': {'waypoint_id': str, 'command_id': str|None} | None,
      #  'suggested_target': str | None}
      def should_replan(self, *, now_ms: int, events: list[str], last_plan_ms: int | None, interval_ms: int = 30_000) -> bool
  ```
- Rules: `observe` uses the waypoint nearest to `perception['pose']` within `arrive_radius_m`. Person visible with `confidence >= 0.5` marks that waypoint `person_seen` and records `last_sighting`. No person marks it `inspected_empty` (only if currently `visited` or `inspected_empty` or `unvisited` and the dog is within radius). `inspected_empty` older than `inspected_ttl_ms` reads as `visited` in `summary` and is eligible again for search after every `unvisited` one. `command_update` with `cmd == 'goto'` and status `accepted|executing` sets `destination`; `completed` marks that waypoint `visited` (unless `person_seen`) and clears destination; `failed` clears destination and returns `'command_failed'`. `next_search_target` returns the nearest `unvisited` waypoint by straight-line distance, else the oldest expired `inspected_empty`, else `None`. `should_replan` is true when `events` is non-empty, when `last_plan_ms` is `None`, or when `now_ms - last_plan_ms >= interval_ms`. A perception whose `pose.map_id` differs from the mission's map is ignored and returns `[]`.

- [ ] **Step 1: Write the failing tests**

```python
# robot/simulation/tests/test_mission.py
import pytest
from robot.simulation.mission import MissionState

WP = [{'id': 'kitchen', 'x': 0.0, 'y': 0.0}, {'id': 'living', 'x': 4.0, 'y': 0.0}, {'id': 'bedroom', 'x': 8.0, 'y': 0.0}]
POSE = lambda x, y, m='house': {'x': x, 'y': y, 'yaw': 0.0, 'map_id': m}

def perception(x, y, person=False, conf=0.9, posture='unknown', location='unknown', ts=1000, frame='f1', map_id='house'):
    return {'person': person, 'posture': posture if person else 'unknown', 'location': location if person else 'unknown',
            'confidence': conf, 'frame_id': frame, 'ts': ts, 'pose': POSE(x, y, map_id), 'caption': 'x'}

def test_new_mission_lists_every_waypoint_unvisited_and_suggests_nearest():
    m = MissionState('house', WP)
    s = m.summary(now_ms=0)
    assert [w['status'] for w in s['waypoints']] == ['unvisited'] * 3
    assert m.next_search_target(POSE(3.5, 0), now_ms=0) == 'living'
    assert s['last_sighting'] is None and s['destination'] is None

def test_completed_goto_marks_visited_and_empty_view_marks_inspected_empty():
    m = MissionState('house', WP)
    assert m.command_update({'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'executing', 'command_id': 'c1'}, now_ms=100) == []
    assert m.summary(now_ms=100)['destination'] == {'waypoint_id': 'kitchen', 'command_id': 'c1'}
    assert m.command_update({'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'completed', 'command_id': 'c1'}, now_ms=200) == ['arrived']
    assert m.summary(now_ms=200)['destination'] is None
    assert m.observe(perception(0.2, 0.1, ts=300), now_ms=300) == ['inspected_empty']
    assert m.summary(now_ms=300)['waypoints'][0]['status'] == 'inspected_empty'
    assert m.next_search_target(POSE(0, 0), now_ms=300) == 'living'

def test_person_seen_records_sighting_and_replans():
    m = MissionState('house', WP)
    events = m.observe(perception(4.1, 0.2, person=True, posture='lying', location='floor', ts=500, frame='f9'), now_ms=500)
    assert events == ['person_seen']
    s = m.summary(now_ms=500)
    assert s['waypoints'][1]['status'] == 'person_seen'
    assert s['last_sighting']['waypoint_id'] == 'living' and s['last_sighting']['frame_id'] == 'f9'
    assert m.should_replan(now_ms=500, events=events, last_plan_ms=490)

def test_low_confidence_person_is_not_a_sighting():
    m = MissionState('house', WP)
    assert m.observe(perception(4.0, 0.0, person=True, conf=0.3, ts=1), now_ms=1) == []
    assert m.summary(now_ms=1)['last_sighting'] is None

def test_inspected_empty_expires_and_becomes_searchable_again():
    m = MissionState('house', WP, inspected_ttl_ms=1000)
    for wp in WP:
        m.observe(perception(wp['x'], wp['y'], ts=10), now_ms=10)
    assert m.next_search_target(POSE(0, 0), now_ms=10) is None
    assert m.next_search_target(POSE(0, 0), now_ms=2000) == 'kitchen'
    assert m.summary(now_ms=2000)['waypoints'][0]['status'] == 'visited'

def test_failed_goto_clears_destination_and_reports():
    m = MissionState('house', WP)
    m.command_update({'cmd': 'goto', 'waypoint': 'bedroom', 'status': 'accepted', 'command_id': 'c2'}, now_ms=1)
    assert m.command_update({'cmd': 'goto', 'waypoint': 'bedroom', 'status': 'failed', 'command_id': 'c2'}, now_ms=2) == ['command_failed']
    assert m.summary(now_ms=2)['destination'] is None
    assert m.summary(now_ms=2)['waypoints'][2]['status'] == 'unvisited'

def test_other_map_perception_is_ignored():
    m = MissionState('house', WP)
    assert m.observe(perception(0, 0, person=True, map_id='garage', ts=5), now_ms=5) == []
    assert m.summary(now_ms=5)['last_sighting'] is None

def test_far_from_any_waypoint_changes_nothing():
    m = MissionState('house', WP, arrive_radius_m=1.0)
    assert m.observe(perception(2.0, 3.0, ts=5), now_ms=5) == []
    assert all(w['status'] == 'unvisited' for w in m.summary(now_ms=5)['waypoints'])

@pytest.mark.parametrize('last,now,events,expected', [(None, 0, [], True), (0, 1000, [], False), (0, 30000, [], True), (0, 1, ['arrived'], True)])
def test_should_replan_rules(last, now, events, expected):
    assert MissionState('house', WP).should_replan(now_ms=now, events=events, last_plan_ms=last) is expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest robot/simulation/tests/test_mission.py -q`
Expected: ImportError / ModuleNotFoundError for `robot.simulation.mission`.

- [ ] **Step 3: Implement `robot/simulation/mission.py`** (dataclass-free plain class, no third-party imports, docstring stating it is deterministic search progress and holds no resident identity). Keep it under 150 lines; statuses as module constants `UNVISITED, VISITED, INSPECTED_EMPTY, PERSON_SEEN`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest robot/simulation/tests/test_mission.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add robot/simulation/mission.py robot/simulation/tests/test_mission.py
git commit -m "feat: add deterministic mission state for systematic home search"
```

---

### Task 2: Posture from keypoints and a person tracker

**Files:**
- Create: `robot/simulation/posture.py`
- Create: `robot/simulation/person_tracker.py`
- Test: `robot/simulation/tests/test_posture.py`
- Modify: `robot/simulation/requirements-perception.txt` (add a comment line naming `yolo11n-pose.pt` provenance; Ultralytics is already listed)

**Interfaces:**
- Produces:
  ```python
  # posture.py (pure)
  UPRIGHT, LYING, UNKNOWN = 'upright', 'lying', 'unknown'
  def classify_posture(keypoints_xy: list[tuple[float, float]], keypoint_conf: list[float], box_xyxy: tuple[float, float, float, float], *, min_conf: float = 0.3) -> dict
  # returns {'posture': 'upright'|'lying'|'unknown', 'torso_angle_deg': float|None, 'reason': str}
  # COCO-17 order: 0 nose,1-2 eyes,3-4 ears,5-6 shoulders,7-8 elbows,9-10 wrists,11-12 hips,13-14 knees,15-16 ankles.
  # torso vector = mean(hips) - mean(shoulders); angle from vertical in degrees.
  # lying: angle >= 60 and box width > box height * 1.1; upright: angle <= 30 and box height >= box width * 0.9; else unknown.
  # unknown when fewer than 3 of the 4 shoulder/hip points have conf >= min_conf.

  # person_tracker.py
  class PersonTracker:
      def __init__(self, *, model_path: str = '.cache/yolo/yolo11n-pose.pt', conf: float = 0.4, device: str = 'cpu', predictor=None)
      # predictor: optional callable(jpeg_bytes) -> list[{'track_id': int|None, 'box': [x1,y1,x2,y2], 'conf': float, 'keypoints': [[x,y],...17], 'kp_conf': [..17]}]
      # when predictor is None, lazily loads Ultralytics YOLO(model_path) and uses model.track(img, persist=True, conf=conf, classes=[0], tracker='bytetrack.yaml', verbose=False)
      def update(self, jpeg_bytes: bytes, *, now_ms: int) -> list[dict]
      # returns tracks: [{'track_id': int|None, 'box': [...], 'conf': float, 'posture': str, 'torso_angle_deg': float|None, 'first_seen_ms': int, 'last_seen_ms': int, 'lying_frames': int}]
      # lying_frames counts consecutive updates with posture == 'lying' for that track_id; resets otherwise; tracks not seen for 3000 ms are dropped.
  ```

- [ ] **Step 1: Write the failing tests** (pure classifier + tracker with an injected fake predictor; no model load)

```python
# robot/simulation/tests/test_posture.py
from robot.simulation.posture import classify_posture, LYING, UPRIGHT, UNKNOWN
from robot.simulation.person_tracker import PersonTracker

def kp(shoulders, hips, conf=0.9):
    pts = [(0.0, 0.0)] * 17; c = [0.0] * 17
    pts[5], pts[6] = shoulders; pts[11], pts[12] = hips
    for i in (5, 6, 11, 12): c[i] = conf
    return pts, c

def test_vertical_torso_in_tall_box_is_upright():
    pts, c = kp([(100, 50), (140, 50)], [(105, 150), (135, 150)])
    r = classify_posture(pts, c, (90, 30, 150, 260))
    assert r['posture'] == UPRIGHT and r['torso_angle_deg'] < 10

def test_horizontal_torso_in_wide_box_is_lying():
    pts, c = kp([(50, 100), (50, 140)], [(150, 105), (150, 135)])
    r = classify_posture(pts, c, (30, 80, 200, 160))
    assert r['posture'] == LYING and r['torso_angle_deg'] > 80

def test_missing_keypoints_is_unknown():
    pts, c = kp([(100, 50), (140, 50)], [(105, 150), (135, 150)], conf=0.1)
    assert classify_posture(pts, c, (90, 30, 150, 260))['posture'] == UNKNOWN

def test_diagonal_torso_is_unknown_not_guessed():
    pts, c = kp([(100, 100), (120, 100)], [(160, 160), (180, 160)])
    assert classify_posture(pts, c, (90, 90, 200, 180))['posture'] == UNKNOWN

def test_tracker_counts_consecutive_lying_frames_and_drops_stale_tracks():
    frames = iter([
        [{'track_id': 7, 'box': [30, 80, 200, 160], 'conf': 0.8, 'keypoints': kp([(50, 100), (50, 140)], [(150, 105), (150, 135)])[0], 'kp_conf': kp([(0,0)]*2, [(0,0)]*2)[1]}],
        [{'track_id': 7, 'box': [30, 80, 200, 160], 'conf': 0.8, 'keypoints': kp([(50, 100), (50, 140)], [(150, 105), (150, 135)])[0], 'kp_conf': kp([(0,0)]*2, [(0,0)]*2)[1]}],
        [],
    ])
    t = PersonTracker(predictor=lambda jpeg: next(frames))
    a = t.update(b'jpeg', now_ms=0)
    assert a[0]['track_id'] == 7 and a[0]['posture'] == LYING and a[0]['lying_frames'] == 1
    b = t.update(b'jpeg', now_ms=500)
    assert b[0]['lying_frames'] == 2 and b[0]['first_seen_ms'] == 0
    assert t.update(b'jpeg', now_ms=4000) == []
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest robot/simulation/tests/test_posture.py -q` → ModuleNotFoundError.
- [ ] **Step 3: Implement `posture.py` (pure, math only) and `person_tracker.py`** (Ultralytics imported lazily inside `_load_model`; JPEG decoded with `cv2.imdecode` or PIL only in the real path; `update` wraps the predictor result into the track dicts and maintains `self.tracks: dict[int, dict]`).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Download the pose weights for the real path** (public asset, keep provenance): `curl -L -o .cache/yolo/yolo11n-pose.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt` and add a line to `robot/simulation/requirements-perception.txt`: `# yolo11n-pose.pt from https://github.com/ultralytics/assets/releases/tag/v8.3.0 (AGPL-3.0, Ultralytics)`. Then a smoke check that is NOT a test: `.cache/dimos/.venv/bin/python -c "from robot.simulation.person_tracker import PersonTracker; import cv2; t=PersonTracker(); img=cv2.imread('output/current-dog-camera.jpg'); ok,buf=cv2.imencode('.jpg',img); print(t.update(buf.tobytes(), now_ms=0))"` and record the wall time in the commit message.
- [ ] **Step 6: Commit** `git add robot/simulation/posture.py robot/simulation/person_tracker.py robot/simulation/tests/test_posture.py robot/simulation/requirements-perception.txt && git commit -m "feat: keypoint posture classifier and tracked person detections"`

---

### Task 3: Semantic episodic memory with goal-conditioned recall

**Files:**
- Create: `robot/app_backend/app/semantic_memory.py`
- Modify: `robot/app_backend/app/main.py` (add `POST /recall` beside `POST /query` at ~line 192)
- Modify: `robot/app_backend/app/service.py` (call `SemanticMemory.index` after the `INSERT OR IGNORE INTO memory` at ~line 319; construct the memory in `__init__` with an embedder from env)
- Test: `robot/app_backend/tests/test_semantic_memory.py`
- Modify: `robot/app_backend/README.md` (document `/recall` and `ANNIE_EMBED_MODEL`, default `nomic-embed-text`, `ANNIE_EMBED_URL` default `http://127.0.0.1:11434`)

**Interfaces:**
- Produces:
  ```python
  class SemanticMemory:
      def __init__(self, db, *, embedder, dims_limit: int = 2048)
      # embedder: callable(list[str]) -> list[list[float]]; may raise; called synchronously.
      # creates TABLE IF NOT EXISTS memory_embeddings (id TEXT PRIMARY KEY, map_id TEXT, ts INTEGER, vec BLOB)
      def index(self, frame_id: str, map_id: str, ts: int, caption: str) -> bool   # False (and no raise) when the embedder fails
      def recall(self, goal: str, *, map_id: str, ts_to: int, limit: int = 6) -> dict
      # {'citations': [{'caption','frame_id','ts','pose','score'}], 'provider': 'embeddings'|'lexical_fallback', 'indexed': int}
      # cosine similarity over rows with matching map_id and ts <= ts_to; captions/pose read from the existing `memory` table payload JSON.
      # lexical_fallback: when the embedder fails for the goal, rank by shared lowercase tokens between goal and caption.
  def ollama_embedder(url: str, model: str, timeout_s: float = 3.0) -> callable  # POST {url}/api/embed {'model','input'} -> data['embeddings']
  ```
- `POST /recall` body `{goal: str(1..500), map_id: str, ts_to: int|None, limit: int(1..6)=6}` → the `recall` dict. 422 on bad input. Requires the same auth as `/query`.

- [ ] **Step 1: Write the failing tests** (in-memory sqlite, fake embedder returning fixed vectors; one test where the embedder raises)

```python
# robot/app_backend/tests/test_semantic_memory.py
import json, sqlite3
from robot.app_backend.app.semantic_memory import SemanticMemory

VEC = {'person on the floor in the living room': [1.0, 0.0], 'empty kitchen counter': [0.0, 1.0],
       'where is the person': [0.9, 0.1], 'kitchen': [0.1, 0.9]}

def fake_embedder(texts):
    return [VEC[t] for t in texts]

def seed(db):
    db.execute('CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
    for fid, ts, cap in [('f1', 100, 'person on the floor in the living room'), ('f2', 200, 'empty kitchen counter')]:
        db.execute('INSERT INTO memory VALUES (?,?,?)', (fid, ts, json.dumps({'caption': cap, 'frame_id': fid, 'ts': ts,
                   'pose': {'x': 0, 'y': 0, 'yaw': 0, 'map_id': 'house'}})))

def test_recall_ranks_by_goal_similarity_with_citations():
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=fake_embedder)
    assert m.index('f1', 'house', 100, 'person on the floor in the living room')
    assert m.index('f2', 'house', 200, 'empty kitchen counter')
    r = m.recall('where is the person', map_id='house', ts_to=1000)
    assert r['provider'] == 'embeddings' and [c['frame_id'] for c in r['citations']] == ['f1', 'f2']
    assert r['citations'][0]['pose']['map_id'] == 'house' and r['citations'][0]['score'] > r['citations'][1]['score']

def test_recall_filters_map_and_time():
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=fake_embedder)
    m.index('f1', 'house', 100, 'person on the floor in the living room'); m.index('f2', 'garage', 200, 'empty kitchen counter')
    assert [c['frame_id'] for c in m.recall('kitchen', map_id='house', ts_to=1000)['citations']] == ['f1']
    assert m.recall('kitchen', map_id='house', ts_to=50)['citations'] == []

def test_embedder_failure_falls_back_to_lexical_and_never_raises():
    db = sqlite3.connect(':memory:'); seed(db)
    calls = {'n': 0}
    def flaky(texts):
        calls['n'] += 1
        raise RuntimeError('ollama down')
    m = SemanticMemory(db, embedder=flaky)
    assert m.index('f1', 'house', 100, 'person on the floor in the living room') is False
    r = m.recall('person floor', map_id='house', ts_to=1000)
    assert r['provider'] == 'lexical_fallback' and r['citations'][0]['frame_id'] == 'f1'
```

Plus an API test in the same file using the existing app test client pattern from `robot/app_backend/tests/test_api.py` (read it first): POST `/recall` returns 200 with `citations` and 422 for an empty goal.

- [ ] **Step 2: Run to verify failure.** `.venv/bin/python -m pytest robot/app_backend/tests/test_semantic_memory.py -q`
- [ ] **Step 3: Implement**, wire `/recall`, index on `observe`. Embedding vectors stored as JSON text in `vec` is acceptable (small). The default embedder in `service.py` comes from env; when `ANNIE_EMBED_MODEL` is unset or `off`, `SemanticMemory` is constructed with an embedder that raises, so lexical fallback is the default in tests.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest robot/app_backend/tests -q` (all must pass) and `.venv/bin/python robot/contract/export_schemas.py --check`; if the check reports a diff because the app contract is exported, run `.venv/bin/python robot/contract/export_schemas.py` and include the regenerated `robot/contract/schemas.json`.
- [ ] **Step 5: Commit** the named files.

---

### Task 4: Brain service context: mission block, current perception, resilient action stage

**Files:**
- Modify: `robot/robot_backend/app/brain/planner.py` (PlanRequest at 99-104, prompts at 53-76, `local_grounded_plan` at 233-287)
- Modify: `robot/robot_backend/app/brain/context.py`
- Modify: `robot/contract/brain.md` (document the optional fields), regenerate `robot/contract/schemas.json`
- Test: `robot/robot_backend/tests/test_context.py`, `robot/robot_backend/tests/test_planner.py`

**Interfaces:**
- `PlanRequest` gains `mission: MissionSummary | None = None` and `current_perception: CurrentPerception | None = None` where
  ```python
  class MissionWaypoint(StrictModel): id: str; status: Literal['unvisited','visited','inspected_empty','person_seen']; age_s: int | None = None
  class MissionSighting(StrictModel): frame_id: str; ts: int; pose: CapturePose; waypoint_id: str | None; posture: str; location: str; confidence: float
  class MissionDestination(StrictModel): waypoint_id: str; command_id: str | None = None
  class MissionSummary(StrictModel): waypoints: list[MissionWaypoint] (max 30); last_sighting: MissionSighting | None = None; destination: MissionDestination | None = None; suggested_target: str | None = None
  class CurrentPerception(StrictModel): tracks: list[TrackSummary] (max 8) where TrackSummary(StrictModel): track_id: int | None; posture: Literal['upright','lying','unknown']; lying_frames: int; conf: float
  ```
- `pack_context` adds `context['mission']` (verbatim summary) and `context['current_perception']` when present; `nearest_waypoint` gains `'note': 'closest by straight line, not necessarily the current room'`.
- Prompts: both `PLANNER_PROMPT` and `LOCAL_PLANNER_PROMPT`, and the system message inside `local_grounded_plan`, gain: "mission lists each waypoint's search status. Prefer goto to an unvisited waypoint; do not goto an inspected_empty waypoint while unvisited ones remain; if last_sighting exists and no person is visible now, goto its waypoint and look. suggested_target is a deterministic hint, not an order."
- `local_grounded_plan`: when the action stage raises `ProviderError`/`ProviderTimeout`/invalid JSON AFTER the vision stage succeeded, return a response whose action is `ActionStep(action='wait', reason='action model unavailable: <ExceptionName>')` and `stats['action_model_error'] = <ExceptionName>`, so the perception is never discarded.

- [ ] **Step 1: Write failing tests** in `test_context.py` (mission and current_perception appear in packed context; nearest_waypoint carries the note; context byte cap still drops outcomes before mission) and in `test_planner.py` (mirror the existing mocked-transport local test: vision succeeds, the `/api/chat` action call returns 500 → response action is `wait` with the reason prefix and the perception fields equal the vision result; a request with an unknown key inside `mission` is rejected with 422).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Regenerate schemas: `.venv/bin/python robot/contract/export_schemas.py`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest robot/robot_backend/tests -q` and `.venv/bin/python robot/contract/export_schemas.py --check`.
- [ ] **Step 5: Commit** the named files only (planner.py and context.py carry a teammate's uncommitted hunks; commit only after the operator confirms, see Task 5).

---

### Task 5: Bridge integration: continuous perception, event-driven planning, mission lifecycle

**Files:**
- Modify: `robot/simulation/bridge.py` (`__init__` ~60-66, `retrieve_memories` 71-91, `process_commands` 243-298, `think` 368-451, `tick` 453-481)
- Test: `robot/simulation/tests/test_continuous_brain.py` (add cases using the existing mocked-HTTP fixtures)

**Behavior:**
1. `self.mission: MissionState | None`; rebuilt whenever `state['map_id']` changes (scene change resets search progress). `self.last_plan_ms`, `self.mission_events: list[str]`.
2. `process_commands` feeds every receipt it emits or observes (`status` in accepted/executing/completed/failed with `cmd`/`waypoint`) into `mission.command_update`, collecting events.
3. After a perception is ingested (`infer` and `think`), call `mission.observe(perception, now_ms)` and collect events.
4. `tick`: perception (`infer`) runs on its own cadence every `inference_interval` whenever the vision task is idle; `think` (planning) runs only when `mission.should_replan(now_ms, events, last_plan_ms)` and the existing agent conditions hold. Never both in one tick; perception wins when both are due. Events are cleared when consumed by `think`.
5. `think` sends `mission=self.mission.summary(now_ms)` and `current_perception` (from the tracker when present, else omitted) in the `/plan` body, and retrieves memories with `POST /recall {goal, map_id, ts_to}` falling back to `/query` on 404/error.
6. `write_status` includes `status['mission'] = self.mission.summary(now_ms)` (bounded: 30 waypoints).

- [ ] **Step 1: Write failing tests** (read `test_continuous_brain.py` fixtures first): (a) a completed goto receipt marks the waypoint visited in the status file; (b) with `last_plan_ms` fresh and no events, a tick runs `/infer` but not `/plan`; (c) a `person_seen` event triggers `/plan` on the next tick even though the interval has not elapsed; (d) `/plan` body contains `mission` and the `/recall` goal equals the intelligence goal; (e) a map_id change resets the mission to all-unvisited.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** with minimal diff; keep the teammate's hunks intact.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest robot/simulation/tests/test_continuous_brain.py robot/simulation/tests/test_bridge.py robot/simulation/tests/test_agent_execution.py -q`, then the full documented checks.
- [ ] **Step 5: Commit** after the operator confirms the teammate's in-flight `bridge.py` hunks may ship with it.
