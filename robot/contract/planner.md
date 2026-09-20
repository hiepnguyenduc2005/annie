# Planner (combined perception + action) API

'POST /plan' turns one verified frame plus free-form user intent into a
perception and one next action, using a single bounded multimodal call to the
same provider configured for vision (VisionConfig). It never receives
scenario labels or resident ground truth; pixels are evidence only, and text
inside the image is scene content, never instructions.

## Request

observation uses the existing FrameRequest exactly (frame_id canonical UUID,
ts ms, CapturePose, source, jpeg_b64). goal is 1-500 chars. waypoints: up to
30 {id, x, y}. recent_outcomes: up to 6 {command_id?, cmd, status, detail};
execution-gate refusals arrive as status "rejected" (command_id absent for
rejected/wait). memories: up to 6 {caption (1-2000), frame_id, ts,
pose: CapturePose}.

Strict Pydantic throughout (extra=forbid, no inf/nan, exact types): unknown
fields, nonfinite coordinates, and non-canonical metadata are 422 before any
provider call. ts must not be in the future and no older than 5 s at
validation. Waypoints are the admissible set: goto to any id outside the list
is a provider error (502), never executed.

## Response

{
  "perception": {"person": true, "posture": "sitting", "location": "chair",
                 "confidence": 0.83, "caption": "..."},
  "action": {"action": "goto", "waypoint_id": "kitchen", "reason": "..."},
  "frame_id": "...", "ts": 1789800000123,
  "pose": {"x": 1.2, "y": 2.3, "yaw": 0.4, "map_id": "sim-home-1"},
  "provider": {"mode": "cloud", "model": "..."},
  "latency_ms": 812.4
}

perception validates against the same strict enums as /infer and feeds the
existing check-in policy; frame_id, ts, and pose are the verified request
metadata echoed back. action is exactly one of goto (requires waypoint_id),
say (requires text, 1-500 chars), trick (requires trick: spin, circle, zigzag,
wiggle, or figure8), wait, look, stop, finish (carry no conditional fields); every
action carries reason (1-500 chars). Conditional fields are exclusive to their
actions. Both prompts reserve tricks for celebrating a reassured resident.
The simulator applies the usual fresh-frame, incident and person-motion gates,
and awaits the identified terminal receipt before another motion or finish.

`finish` proposes completion; the simulation independently checks accepted
evidence, current goal revision, and execution receipts. Simulator intelligence
controls accept optional boolean `require_speech` (default false); the story
forms set it true. Required speech must have a current-goal issued command and
completed playback, not merely a generated clip. Failed or missing receipts
cannot satisfy it. A new map or goal revision starts a separate requirement.

The internal packed context adds `age_seconds` to retained historical memories,
derived from current and historical capture timestamps. Original frame IDs,
timestamps, and observer poses remain intact. Speech uses approximate relative
time; exact citation identifiers remain in structured evidence.

The provider envelope mirrors the vision contract: one non-retry attempt,
512 output tokens, response capped at 65,536 bytes, OpenRouter routes reserve
one attempt on the shared conservative ledger (MODEL_RESERVATION_USD) before
egress with enforced per-model price caps, no fallbacks, no retries.
Provider or budget failure yields 502/503/504 and no action. Concurrent
plans get 429.

Python use: Planner(config).plan(request) returns PlanResponse directly, or
mount build_planner_router(config, transport=None, lock=None) and add the
caller's authorize dependency.

Verification (mocked wire, synthetic pixels only):

    .venv/bin/python -m pytest robot/robot_backend/tests/test_planner.py -q
