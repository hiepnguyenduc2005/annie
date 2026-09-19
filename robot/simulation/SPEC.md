# Annie SDK and simulation specification

Status: working proposal, 2026-09-19. The first scenario is **patrol → possible
incident → resident check-in → family attention**. This document defines what
to build and measure; [SDK findings](../../docs/SIMULATION_FINDINGS.md) record what
has actually run. The software dashboard's synthetic scenarios are a separate
layer from MuJoCo or DimOS execution.

## Purpose

Prove that Annie's components communicate correctly, identify where the real
SDK helps, and expose the missing pieces before connecting a physical dog.
The final demonstration should answer:

1. Where was the robot looking, and when?
2. What evidence caused a check-in, and how uncertain was it?
3. Did the resident respond, did the family receive the event, and was a reply
   merely queued or actually played?
4. Does safe bed-rest avoid an alert while a sustained floor observation gets
   attention without repeated alarms?

## Separate validation layers

| Layer | Real thing under test | What passing does not establish |
| --- | --- | --- |
| A: software mock | API, event policy, persistence, app, provider failure handling | Physics, motion, camera perception, or voice playback. |
| B: MuJoCo physics | Actual Go2 model, joints, contacts, finite simulation steps | DimOS navigation, a trained controller, SLAM, or a fall detector. |
| C: SDK integration | DimOS simulation connection, command/topic flow, synchronized camera/pose | Real hardware timing or accurate interpretation of a synthetic person. |
| D: navigation and map | Simulator map/pose, three waypoints, pause/resume, obstacle behavior | A surveyed house map or stable identity across map resets. |
| E: perception | Local image-capable VLM receives rendered frames and returns validated observations | Clinical fall-detection accuracy or proof of unconsciousness. |
| F: full scenario | Simulated observations drive check-in, app, actual configured voice/notification adapters | Production readiness or real emergency-response reliability. |

Track layers independently. A recorded JSON fixture is useful for layer A but
cannot pass layers B–F. A ground-truth label must be labeled as such and cannot
be scored as VLM perception.

Current interactive entry point: [live viewer setup](README.md), port 8766.
It implements direct MuJoCo inspection, a scene factory, trained Go1-surrogate walking, authored-map A* waypoint missions, a robot-mounted camera, app command receipts, and speech synthesis/playback acknowledgements. The full DimOS module/navigation stack remains a separate uncompleted integration. Image/audio inference runs behind the [brain contract](../contract/brain.md), with actual run results recorded separately.
The inspected DimOS legacy simulator maps `unitree_go2` to a Go1 model/policy;
the direct viewer uses the actual Go2 asset. Record this distinction in runs
instead of treating both paths as identical robot dynamics.

## World and actors

Start with one floor and a small scene: bedroom/bed, adjacent floor area,
passage, and a family demo viewport. Include a chair, bedside table, lamp,
glasses/mug props, and one movable obstacle only if they exercise a scenario.
Reuse upstream Go2 assets and simulator integration rather than implementing
robot physics, SLAM, or locomotion from scratch.

| Entity | Minimum representation |
| --- | --- |
| Robot | Upstream Unitree Go2 MuJoCo model; measured simulated body/camera pose. |
| Resident | Clearly labeled synthetic stand-in with controllable bed/floor posture; realism of VLM perception must be evaluated separately. |
| Bed | Fixed collision geometry and explicit world transform. |
| Floor | Collidable plane, distinct from bed surface. |
| Three waypoints | Living area, bedroom observation point, and passage; collision-free reachable goals, with reproducible IDs. |
| Occluder | Optional chair/box for unknown-person and dynamic obstacle cases. |
| Home map | SDK occupancy/cost map if available; a drawing from scene geometry must be labeled synthetic, not SLAM. |

Scene dimensions, camera extrinsics, map origin, and starting pose must live in
a versioned scenario/config file when the scene is built. Avoid hardcoding a
person's actual location as the robot's observation pose.

## Time, identity, and sensor contract

- Use simulation time for reproducibility and a run-start epoch to convert to
  Unix milliseconds at the Annie adapter. Log both time domains in local run
  metadata; never compare raw simulation seconds to host Unix milliseconds.
- Pin SDK commit, model asset source/revision, MuJoCo version, scenario version,
  random seed, physics timestep, and adapter version in each run report.
- Each frame has a UUID, capture timestamp, camera identifier, map ID, and the
  synchronized observer pose. Record frame-to-pose skew; reject stale frames.
- Target 640×480 RGB at 2 Hz for the perception pipeline; native physics rate
  is independent. Measure achieved rate and latency rather than assuming them.
- Keep source labels: `mock`, `simulation_ground_truth`, `simulation_vlm`, or
  `hardware_vlm` in the run metadata and UI context. Existing API payloads may
  remain v0.1; adapters must not smuggle new fields past strict validation.
- The current app consumes planar observer `(x,y,yaw)`; retain full 3D pose and
  calibration locally if SDK outputs it. Do not market planar observations as
  a completed 3D-plus-time knowledge graph.

## SDK adapter behavior

The body adapter should publish status/map and provide local frames to the
brain. The brain adapter runs local inference and emits `brain.perception`.
The API consumes the validated contract and owns the current demo policy.
Do not run two incident engines on the same stream.

Adapter responsibilities:

1. Explicit simulation-only configuration; no fallback to a physical robot
   endpoint if the simulator is absent.
2. Translate SDK values into [contract v0.1](../contract/README.md) with explicit
   units/map IDs, without guessing absent confidence or geometry.
3. Publish availability and failures honestly. A disconnected simulator cannot
   look like an idle connected robot.
4. Attach command IDs and report accepted/executing/completed/failed only when
   corresponding SDK evidence exists. The current API's `queued` is not enough.
5. Stop the simulation cleanly, close processes/connections, and preserve a
   bounded run report. Physical emergency stop stays outside this software path.

Redis is the proposed later local transport. First validate one process/adapter
through `/ingest`, then add Redis without changing payload semantics. Avoid
making Redis a prerequisite for a physics-only smoke test.

## Perception modes

**Ground-truth mode:** scenario labels drive the API directly to validate
policy and UX. No VLM claim. Use this mode to develop in parallel.

**Local model mode:** rendered RGB is sent to the verified image-capable model
on GX10 or another explicitly configured local endpoint. Validate person,
posture, location category, confidence, and caption. Set a three-second request
deadline; a timeout is missing evidence, never an incident label. Keep ambiguous
location unknown. No automatic cloud fallback. The user explicitly authorized cloud inference for synthetic simulator images/audio and a combined $20 external inference cap. Hardware frames are rejected by cloud mode.

Bed semantics can be visually inferred for the prototype but must be evaluated
against known scene cases. A later geometric method would need calibrated
depth and transforms; neither VLM distance nor robot pose supplies that alone.

## Scenario matrix and acceptance

| ID | Scenario | Required result |
| --- | --- | --- |
| SIM-001 | Start/reset simulator | Model loads, time advances, finite states; run source/version recorded. |
| SIM-002 | Patrol three waypoints | Goals reached in order without scene penetration; report actual controller/navigation path used. |
| SIM-003 | Pause/resume and software stop | Command acknowledgement and observed motion state agree; separate physical e-stop. |
| SIM-004 | Resting on bed | No incident over a sustained ten-second observation window. |
| SIM-005 | One transient non-bed lying tick | No escalation; a second qualifying distinct tick is required. |
| SIM-006 | Two floor ticks, no response | One suspicion, one spoken request if configured, then one escalation after the eight-second deadline. |
| SIM-007 | Confident correlated "I'm okay" | Check-in closes; no escalation for that episode. |
| SIM-008 | Correlated "help" | Immediate escalation; one incident, not one per frame. |
| SIM-009 | Unrelated/old/ambiguous speech | Does not clear the active incident. |
| SIM-010 | Low confidence, occlusion, unknown location | No invented safe/unsafe classification or incident from missing evidence. |
| SIM-011 | Delayed, duplicated, future or reordered frames | No false event; fresh valid frames resume normal processing. |
| SIM-012 | VLM timeout / SDK disconnect | Error visible; no perception invented; existing check-in timer still resolves. |
| SIM-013 | App disconnect/reconnect | Refetch events/status, deduplicate by ID, preserve acknowledgements. |
| SIM-014 | Memory hit and miss | Hit cites observation, timestamp and observer pin; miss explicitly unanswerable. |
| SIM-015 | Map reset | New map ID; old coordinates not presented as belonging to the new map. |
| SIM-016 | Media boundary | Full frames absent from app stream and routine logs; cloud requests carry only explicitly configured synthetic simulator media. |
| SIM-017 | Optional provider unavailable | Local check-in policy continues; no silent paid fallback. |

For layers involving physics, preserve a short visual capture or measurable
telemetry alongside the test result. App tests alone do not pass the physics
rows. Failure cases should be reproducible with a fixed seed and scenario.

## Metrics

Record incident-label arrival → check-in event → escalation → app receipt →
external notification receipt separately. Record request/response latencies,
frame drops, timeout count, frame-to-pose skew, and waypoint completion. Count
false alerts only over an explicitly listed negative scenario set; report the
denominator and test mode. Do not claim "fall-to-call" if no actual call occurs.

## Out of scope and exit criteria

No stairs/two floors, new locomotion/SLAM algorithm, robot arm, fine-tuning,
clinical certification, multi-resident identity, or hospital dashboard. Do not
add Ansys unless a specific engineering calculation needs it.

The SDK exploration milestone is complete when supported setup commands,
actual execution evidence, platform blockers, and a reproducible next run are
recorded. The full simulation milestone requires SIM-001 through SIM-017 with
explicitly stated supported layers; incomplete layers remain open tasks.

## Brainstorming decisions

Working preference: patrol/check-in is the first demo; family Q&A is the second
scene; reminders follow only after both work. The main product choices to
discuss are whether the robot checks in proactively or waits for family, what
reassurance can safely close a check-in, and which observation is meaningful
enough to share. Keep these separate from simulator plumbing.

## Implemented motion and multimodal boundary (2026-09-19)

Walking uses the matched upstream Go1 model and ONNX policy, with real joint
actuation and measured MuJoCo displacement. The original Go2 model remains
available without `--locomotion`. The planner uses authored collision geometry,
not perception or SLAM. It performs bounded waypoint/patrol/stop/resume/look
missions, fails if blocked or progress stops, and reports identified receipts.
Robot-camera JPEGs and poses are captured together. Interactive walking is not
stopped by the static scene observation schedule; each mission has its own
150-second limit. The scene schedule does not animate residents.

Cloud camera experiments use DeepSeek V4.1 Flash; the catalog does not expose
a V4.1 Pro vision model. MiMo V2.5 is the candidate for image/audio input. Their
latencies and actual modality behavior must be measured. A provider accepting
an HTTP request is not proof it received the media or transcribed correctly.
Local Qwen3-VL is an explicit alternative for comparison, with no automatic
switching. Expired results remain visible as observations but cannot trigger
fresh incident-policy decisions.

Speech synthesis uses macOS `say` for the current laptop simulation, returning a
WAV and a synthesized receipt. Only a browser `ended` event reports playback
completion. This is laptop audio, not a speaker on real dog hardware. Synthetic
WAV transcription has a separate request contract; it does not silently create
resident reassurance or commands.
