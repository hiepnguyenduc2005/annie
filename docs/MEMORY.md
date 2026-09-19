# Episodic memory

`robot/app_backend/app/episodic_memory.py` provides spatiotemporal retrieval over
perception frames, grounded in the ReMEmbR approach (captions bound to capture
time and robot pose; see https://github.com/NVIDIA-AI-IOT/remembr and
https://arxiv.org/abs/2409.13682). It is deterministic: instead of ReMEmbR's
LLM agent it uses lexical entity matching, time windows, and waypoint
proximity, so every answer cites stored frames and unsupported questions are
refused rather than inferred. No paid API calls, no raw images stored.

## What it provides

- `EpisodicMemory(db, now=..., map_id=..., waypoints=..., radius_m=...)` reads
  the existing bounded `memory` SQLite table written by `Service.observe`
  (frames keep `frame_id`, `ts`, pose, caption, `source`, `model`). It never
  writes. `Service.query` uses this retriever with the current map, waypoints,
  and the service clock.
- `ask(text)` answers three query families with citations (`frame_id`, `ts`,
  `pose`, `source`, `model`, `map_current`, `historical`):
  - **where**: latest relevant observation, near a mentioned waypoint when one
    is named, with map discrimination ("not the current map" labeling).
  - **when / last seen**: latest matching frame with UTC time and age; the
    "last seen" aggregate is scoped to the mentioned entity.
  - **how long**: elapsed time between the first and last matching captures,
    cited with both endpoints; continuous presence is explicitly not claimed,
    and a single capture is refused (`duration_unknown`) rather than reported
    as a duration.
- Refusals: unknown entities return `answerable: false` with no citations;
  "who" questions are refused outright (captions carry no identity and none is
  inferred); future-dated frames are excluded and reported via
  `excluded_future`; observations older than a day remain valid but cite
  `historical: true`; a waypoint filter with no current-map match refuses
  with `kind: not_near` rather than borrowing coordinates from another map.
- Matching is all-tokens: every meaningful content token must appear in the
  caption (bounded trailing-s singular normalization on both sides), so a
  "blue umbrella" query does not false-hit a "blue cup" caption. Bare numbers,
  time expressions ("3 minutes", "today"), "ago", and sighting verbs
  ("visible", "seen", ...) are query constraints or question words, never
  required caption tokens. The robot pose in answers and citations is the
  observer pose; object location is only what the caption states.
- `now` defaults to a millisecond clock (`int(time.time() * 1000)`) to
  match frame timestamps; pass a custom callable for tests. Future-dated
  frames are always excluded (`FUTURE_SKEW_MS = 0`).

## API integration

`POST /query` calls `Service.query`, which supplies the current map and
waypoints to `EpisodicMemory.ask`. There is no fallback to partial-token
matching when the grounded retriever refuses a question.

## Guarantees and limits

- Every citation is a stored frame ID and capture timestamp; the module never
  generates captions, identities, or poses.
- Retrieval is lexical, so synonyms miss (same limitation as the existing
  keyword query). An embedding backend can slot behind `ask` later.
- The map-current label is advisory; ingest already rejects frames from other
  maps, so old-map hits only arise from earlier sessions with a different
  `map_id`.

17 tests in `robot/app_backend/tests/test_episodic_memory.py` cover retrieval,
refusals, future/historical handling, and the adapter over a real `Service`
database.
