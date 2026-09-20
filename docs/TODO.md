# Annie TODO

Use this file for actionable work. Each task needs a clear completion condition;
link to specification IDs when requirements exist. Move tasks between sections
as work progresses and record a reason for blocked work.

## Now

- [ ] TASK-014: Intelligent dog brain, step 1 (design: `superpowers/specs/2026-09-19-intelligent-dog-brain-design.md`; plan: `superpowers/plans/2026-09-19-intelligent-dog-brain-step1.md`). Done when the simulated and physical dog search systematically, trigger the fall check-in from two independent signals, listen to a live reply, and local plans pass the 5 s gate.
  - Shipped 2026-09-19 (offline tests, documented checks pass): `robot/simulation/mission.py` (search progress), `posture.py` + `person_tracker.py` (keypoint posture, ByteTrack), `app_backend` `POST /recall` (embedding recall with lexical fallback), `robot/go2_perception.py` (hardware camera + posture + cited inference, no motion), `robot/simulation/live_listener.py` (VAD-gated microphone reply, `source: microphone`), `robot/go2_host_voice.py` (host speech + listening with `source: host` receipts; verified end to end on the Mac against the app).
  - Open: wire `MissionState` and `/recall` into the planner/bridge (Codex owns `planner.py`, `context.py`, `bridge.py`, `task_progress.py`); benchmark mlx-vlm runtimes for sub-second local perception; a `person_seen` waypoint never retires, so cap the "return to last sighting" rule; test the posture rule on dog's-eye footage of a person on the floor; hardware runs need the dog in range and charged above 40%.

- [ ] TASK-006: Complete and measure live image/audio inference behind the GX10-compatible service. Keep stale perception out of incident rules; distinguish successful media interpretation from HTTP acceptance.
  - Verified: two full-house model-driven simulator runs completed walking, camera detection, native check-in, correlated recorded reply, family alert and family playback in 41.864 s and 37.974 s. [Evidence](LIVE_DEMO.md).
  - Remaining: full scenario/latency qualification, real microphone input and GX10 deployment.

## Next

- [ ] TASK-004: Connect and evaluate GX10 local image inference. Done when rendered frames produce valid measured observations and the scenario matrix distinguishes VLM results from ground truth.
- [ ] TASK-005: Integrate actual speech, messaging, and Elastic adapters. Done when provisioned services acknowledge execution and privacy boundaries are verified with synthetic data.
  - Current deployment direction: all local, including Elastic. [Environment template and setup](LOCAL_ENV.md) reuse Ollama, Whisper/macOS speech, and Graphiti; self-hosted Elasticsearch needs a node, index, local API key, CA certificate, and live write/search verification. Cloud-only voice/messaging adapters are deferred for this profile.
- [ ] TASK-006: Implement `robot_backend`'s side of `contract/family_messages.md` (`POST /dispatch`, `POST /internal/events` calls). Done when a real navigate/speak/listen/recall/speak run on the GX10 posts live events into `app_backend` and completes without `ANNIE_FAMILY_MOCK_ROBOT`.

## Blocked

- [ ] TASK-007: Physical Go2 patrol within the operator's requested five-metre boundary (REQ-001; HW-04–HW-06).
  - Verified: V3 authentication, Wi-Fi AP, 720p camera, measured pose/battery, LiDAR, firmware 1.1.15, and enabled obstacle avoidance. See [hardware setup](../robot/SETUP.md).
  - Prepared: [offline patrol supervisor](../robot/patrol/README.md), with 44 pure gate tests and 11 mocked bridge cases; physical adapter integration remains open. No verification flags are inferred from stationary acknowledgments.
  - Current: operator corrected the earlier report: no physical controller is available. BLE advertises, but GATT connection times out before authentication; neither saved hotspot appears and LAN discovery is empty. No fresh battery reading or new motion. MIT is the target demo network; the robot's current address there is unverified. Last historical battery was 23%; follow Unitree’s below-40% operating guidance.
  - Blockers: independent stop/recovery, moving-stop and link-loss behavior, physical map alignment, and enforced boundary are unverified. MCF uses `error_code` for gait state; `mode=0` is not idle proof. Small pose changes, acknowledgments and offline driver tests do not satisfy execution checks.
  - `go2_walk.py` enforces a 40% minimum battery threshold and an active local CLI motion inhibit; 45 fake-only tests pass. Before hardware use, clear competing launch assignments, fix its duration-only completion flag, require distinct fresh stop observations, and verify the MCF motion path. Physical communications/hotspot recovery is the next dependency.
  - Progress 2026-09-19 22:22: first supervised physical motion. A 1 m line at 0.3 m/s completed with a 34 ms StopMove ack and 1.7 cm overrun; a 1 m-radius circle ran 34 s until the 40% battery floor stopped it. Route: dog on the operator's phone hotspot, Mac tethered by USB, venue Wi-Fi untouched. See [hardware setup](../robot/SETUP.md).
  - Done when: a nearby operator can reliably stop the robot and one deliberately slow, bounded physical route completes with measured pose and execution evidence.

- [ ] TASK-008: Persist profiles server-side (MongoDB was proposed) so the app and dog profiles, their kinds, and which was created first survive across devices.
  - Requirement or context: REQ-012, DEC-008. The SwiftUI registration screens already exist and store profiles on the device.
  - Done when: registration writes to the shared backend; the create-first rules are decided and tested; the app reads profiles back after reinstall.
  - Dependency or blocker: the backend owner's requirements for profile storage and API shape, and which service owns it. No MongoDB is installed locally; the swift mock backend has no MongoDB driver.

## Done

- [x] Recovered simulation planner after the cloud reservation cap produced HTTP 503. The user raised the total allowance to $50 ($49 shared ledger plus the existing separate $1 probe); prior usage is preserved. Fixed actionable availability errors, stopped automatic retries for configuration/budget failures, and removed the misleading Ready label. Live exploration again found the resident and completed spoken playback.

- [x] TASK-015: Live Zach → Annie → Janine story completed twice (REQ-014; [measurements](../robot/simulation/STORY_DEMO.md)): model-selected walking, camera person detection, Zach's reminder played, then a Graphiti-grounded phone answer played. The guarded repeat took 21.779 s / 11.606 s, with 7 model calls and 1.827 s median provider latency. Janine's reply is demo-actor text; memory includes real inferred setup and robot-camera captures.

- [x] TASK-013: Added interactive camera controls, an operator workspace, and DimOS-derived raycast LiDAR, measured-trail and planned-route layers (REQ-013). Browser-verified orbit/pan/zoom/reset and layer acknowledgement; toggling layers changes operator frames while robot-camera frames remain byte-identical.

- [x] TASK-001: Recorded SDK launch evidence and platform/asset blockers; direct MuJoCo and trained Go1-surrogate motion run locally. Full DimOS integration remains separate.
- [x] TASK-003: HTTP body adapter publishes simulated maps/poses and forwards app commands with execution receipts.
- [x] Added deterministic furnished scene batches, realistic cached assets, measured pacing, and verified Blender export.
- [x] Added trained-policy waypoint/patrol movement and robot-camera rendering; no kinematic pose teleporting.

- [x] TASK-002: Integrated the split app service, phone-friendly dashboard, and optional advisory provider. 47 automated checks passed; browser verified bed exemption, reassurance, timeout, acknowledgement, queued message, memory hit/miss, and mobile layout.
- [x] TASK-007: Implemented app_backend's side of the family message relay (`POST /api/messages`, `GET /api/runs/{run_id}`, `GET /api/thread`, `WS /ws/family`, `POST /internal/events`) per `contract/family_messages.md`, plus a mock-mode dispatch path and connectivity/exercise scripts. 67 automated checks passed; both real dispatch outcomes (success and robot-unreachable, ~0.8s to resolve) and the mock-mode full sequence were run and verified against a live server, not just unit-tested. `robot_backend`'s side remains TASK-006.

- [x] Create the initial documentation scaffold and shared agent instructions.
- [x] Preserve all supplied HackMIT resources and planning notes; push source capture (`804aa65`).

Keep completed entries brief; use Git history for detailed change history.

## Task format

Copy this template into the relevant section when there is actual work:

```markdown
- [ ] TASK-001: Concrete outcome.
  - Requirement or context: link to the relevant specification section.
  - Done when: observable result and how to verify it.
  - Owner: add when coordinating multiple contributors.
  - Dependency or blocker: add only when relevant.
```
