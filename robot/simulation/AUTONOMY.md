# Bounded autonomous mission coordinator

robot/simulation/autonomy.py is the autonomy brain for the continuous
home-elderly simulation demo. It is deterministic and transparent: plain
Python rules over measured inputs, no LLM, no network calls, and no camera
ground truth or resident routine posture/activity as decision inputs. A
future model adapter would live outside this module; none is wired.

## API

    from robot.simulation.autonomy import Autonomy

    autonomy = Autonomy(capabilities={"upstairs": False})  # default: False
    autonomy.set_mode("patrol")   # operator-select, raises ValueError otherwise
    command = autonomy.decide(status, map_data, viewer_state, now_ms)

- set_mode accepts "patrol", "watch", "paused", "find_resident". The
  instance starts in "paused".
- decide returns None or exactly one command:

      {"cmd": "goto" | "stop" | "look", "token": "auto-0001",
       "mode": "<current mode>", "reason": "<stable short reason>",
       "waypoint": "<map waypoint id, goto only>"}

  Tokens are unique per issue and are the correlation key for receipts.

## Inputs (measured, not assumed)

- status: the app GET /status body (dog with ts, state, waypoint, pose;
  pending_checkin when a check-in is unresolved).
- map_data: the ingested dog.map (map_id, waypoints with id,x,y; floor,
  level, and z fields, if present, are honored but are upcoming metadata).
- viewer_state: live viewer state plus the navigation and person_safety
  objects from the viewer snapshot, plus receipts: a dict of
  {token: "accepted" | "executing" | "completed" | "failed"} that the
  integrator fills from app command receipts.
- now_ms: caller-supplied epoch milliseconds; the module keeps no clock.

## Policies

- Command rate is bounded: at most one command per 2 s (a gap of at least
  2000 ms between issues).
- Telemetry older than 5 s, future-dated telemetry, or a pose/map identity
  mismatch means no fresh evidence: issue stop once, then hold silently.
- pending_checkin latches a hold immediately (never approach). The hold
  persists after resolution until the operator explicitly re-selects a
  mode.
- A camera person sighting (person_safety ready with detections) latches
  stop/hold and records the nearest measured waypoint plus frame id and
  capture time as last_observed. Fresh clear frames alone never release
  the latch; an explicit operator re-selection does, and even then a
  fresh clear view and no pending check-in are required.
- Patrol visits named reachable map waypoints in order, starting from the
  neighbor of the measured nearest waypoint, and never re-targets the
  occupied waypoint.
- goto progression is receipt-driven and pose-verified: the next command
  waits for the app receipt completed AND the measured pose within
  0.75 m of the target. A failed receipt, or a completed receipt without
  measured arrival, latches a hold until explicit operator resume. No
  blind retries, ever.
- find_resident runs one bounded episode per explicit entry: goto the
  last observed waypoint (only if it is a known current-map waypoint),
  then at most one look scan. Any failure ends the episode. Without a
  recorded observation the episode does nothing. The mode never
  self-rearms.
- watch and paused never initiate motion.
- Waypoints with floor/level/z above ground are excluded unless
  capabilities={"upstairs": True} is explicitly passed. Unknown floor
  metadata on future multi-floor maps stays off-limits by default.
- Freezing conditions: person stop, detector error, fall states, and
  pending check-in all surface as hold reasons; dog.state "estop"
  additionally holds. Motion only ever starts when telemetry is fresh,
  no hold is active, and the mode authorizes it.

## Testing

    .venv/bin/python -m pytest robot/simulation/tests/test_autonomy.py -q

Ten tests cover the patrol cycle and rate bound, watch/paused immobility,
person stop/hold/record, check-in hold-until-resume, stale telemetry,
map identity mismatch, non-ground waypoints, failed-goto latching,
find_resident single episode, and receipt+pose-verified progression.
