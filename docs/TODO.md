# Annie TODO

Use this file for actionable work. Each task needs a clear completion condition;
link to specification IDs when requirements exist. Move tasks between sections
as work progresses and record a reason for blocked work.

## Now

- [x] Add shared Annie speech mute to the family app and operator dashboard. Controls use
  confirmed server state; muting cancels owned Mac playback and queued synthesis, closes
  the open conversation window, and preserves microphone commands and robot controls.
  Muted spoken missions fail honestly. Verified with mocked voice/device/runtime/API tests
  and a signed physical iPhone build. The separate phone-audio socket cannot cancel bytes
  already sent. Physical setup remains subject to the telemetry dropout noted in TASK-019.

- [ ] TASK-019: Qualify the user-authorized autonomous demo end to end on hardware (SPEC "Demo casting"; DEC-016).
  Current mode: `--demo-everyone-grandma --autonomous-demo --voice --idle-trick 45 --speed 0.2
  --boundary 5 --target ''` (user override): every visible person is labeled Jeanine
  (`demo_role`, no identity recognition); `--autonomous-demo` requires everyone mode and is
  rejected with manual-control or paused startup; everyone mode alone (and clothing selection)
  keeps the held/manual start. Model inference is ON via the local Ollama text planner
  `annie-qwen3-vl:2b`; Deepgram/ElevenLabs cloud voice is configured, with mic/speaker on the
  Mac host, not the robot (so "no cloud provider" means the model only).
  - Physical evidence: stand completed with code 0 and the user confirmed the dog upright; a
    short autonomous patrol measured 0.19 m, then the autonomous pose measured ~1.5 m from
    origin; no full patrol/navigation qualification yet. Physical wave and Mac speech were
    confirmed earlier by the user. Interactive-restart stop ran with code 0 and a 38.4 ms
    firmware ack; an acknowledgment is not a measured stop. Runtime is currently held/manual
    between commands (restored for the upright check); the operator's active Go/explore requests
    still work in that state.
  - Verified family delivery: real iPhone requests reached the robot through app API :8021 →
    errand :8022 → dog :8011. The reminder run completed (found/speaking/listening/heard/
    completed); the captured speech was ambient conversation, so the reply is recorded unclear,
    not an intended Grandma response. The check-in run likewise heard ambient speech
    ("unclear"). Transcripts stay out of tracked files.
  - Verified in code/tests (fake bodies): latest social/control suites 11 pass; the two new
    runtime regression tests passed separately; agent planning/conversation checks were 88 pass
    previously. Voice listener and host-voice checks passed 26 mocked tests; a separate runtime check confirms
    unaddressed dialogue cannot enqueue movement. The
    unsupported flip/rollover guard passed 41 planner tests. Keep still between errand
    approach/say/listen and the 8 s blocked-follow guard are implemented and documented
    (SPEC, DEC-016).
  - The converse → rule_plan removal, unsupported-flip guard, and voice capture-generation
    fix have been reloaded. Full physical voice qualification remains open. During the mute
    rollout, held/manual sessions received camera, LiDAR and battery (59–60%), then stopped
    because pose or lowstate telemetry exceeded its one-second freshness guard. Five ping
    samples measured 7–816 ms with no loss; that does not establish control-link stability.
    An independently restarted controller was also observed during reconnects; single
    controller ownership and stable telemetry remain open. Do not resume motion until resolved.
  - Not done: no full autonomous patrol qualification, no all-green claim, no production
    signoff. Source evidence stays in ignored `.data/hardware/` logs (autonomous-demo and
    interactive-demo sessions); private info, secrets, and machine URLs are not copied into
    tracked files.

- [ ] TASK-018: Finish the physical-phone acceptance pass after unlock ([handoff](PHONE_DELIVERY.md)).
  - Implemented and committed: inline reminder progress, shared family refresh, duplicate suppression, bounded missing-event failure, Pause, correlated firmware stop receipts, and visible ElevenLabs/Deepgram configuration.
  - Verified: 108 family API checks, 344 robot app/backend checks, 111 errand checks, native builds and model/router checks; API-driven native simulator shows executing → cancelled → a fresh request delivered in 24.909 s with mock audio. Physical family Pause acknowledged in 129.7 ms. Updated signed app installed.
  - Physical follow-up: actual iPhone requests now reach the physical robot and Mac audio; see TASK-019. The intended resident reply and sustained physical operation still need qualification.

- [ ] TASK-017: Complete the Claude handoff as an iPhone demo backed by the executing simulator.
  - Scope: conversation with mocked audio, People/re-identification, MCP/care skills, app polish, isolated launch, and seeded message/fall scenarios. See [runbook](SIM_DOG.md).
  - Done when: the final verification pass succeeds, the iPhone visibly connects to the simulated dog, actual HTTP missions terminate honestly, and reviewed changes are pushed.
  - Verified: iPhone reminder delivered with mocked resident reply (29.8 s); full robot suites passed in both environments (545/550 tests before the final paused-telemetry regression). Message sweep 20/20. Fall sweep 16/20: duplicate check-ins at seeds 1/5/8 and synthetic-person contact at seed 15 remain open; autonomous readiness is not complete.
  - Physical follow-up remains separate: live microphone/ElevenLabs, Go2 motion/stop/link-loss qualification, requested door contact, device signing, and GX10 access.

- [ ] Configure and verify the companion final-summary receiver on port 8000 when API delivery is re-enabled. Delivery is currently disabled; local finalization and outbox retention are covered by mocked tests.

- [ ] Verify the standalone `speaker_mic` app connects on a physical phone using its build-configured `/audio` URL. Address/key fields removed; simulator build verified. Run `robot_backend` on port 8080, then check LAN connection and playback on device.

- [ ] TASK-016: Rehearse the one-command local stack and HTTP showcases ([runbook](LIVE_DEMO.md#one-command-local-showcases)). Launcher, showcases, trick bridge/planner support and offline regression checks are implemented. Done when the local patrol, fall timeline and five tricks complete with recorded live timings. Current coding sandbox blocks localhost HTTP and Git writes; live rehearsal and delivery commits remain pending.

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
- [ ] TASK-009: Implement `robot_backend`'s side of `contract/family_messages.md` (`POST /dispatch`, `POST /internal/events` calls). Done when a real navigate/speak/listen/recall/speak run on the GX10 posts live events into `app_backend` and completes without `ANNIE_FAMILY_MOCK_ROBOT`.

## Blocked

- [ ] TASK-007: Physical Go2 patrol within the operator's requested five-metre boundary (REQ-001; HW-04–HW-06).
  - Verified: V3 authentication, Wi-Fi AP, 720p camera, measured pose/battery, LiDAR, firmware 1.1.15, and enabled obstacle avoidance. See [hardware setup](../robot/SETUP.md).
  - Prepared: [offline patrol supervisor](../robot/patrol/README.md), with 44 pure gate tests and 11 mocked bridge cases; physical adapter integration remains open. No verification flags are inferred from stationary acknowledgments.
  - Current: operator corrected the earlier report: no physical controller is available. BLE advertises, but GATT connection times out before authentication; neither saved hotspot appears and LAN discovery is empty. No fresh battery reading or new motion. MIT is the target demo network; the robot's current address there is unverified. Last historical battery was 23%; follow Unitree’s below-40% operating guidance.
  - Blockers: independent stop/recovery, moving-stop and link-loss behavior, physical map alignment, and enforced boundary are unverified. MCF uses `error_code` for gait state; `mode=0` is not idle proof. Small pose changes, acknowledgments and offline driver tests do not satisfy execution checks.
  - `go2_walk.py` enforces a 40% minimum battery threshold and an active local CLI motion inhibit; 45 fake-only tests pass. Before hardware use, clear competing launch assignments, fix its duration-only completion flag, require distinct fresh stop observations, and verify the MCF motion path. Physical communications/hotspot recovery is the next dependency.
  - Progress 2026-09-19 22:22: first supervised physical motion. A 1 m line at 0.3 m/s completed with a 34 ms StopMove ack and 1.7 cm overrun; a 1 m-radius circle ran 34 s until the 40% battery floor stopped it. Route: dog on the operator's phone hotspot, Mac tethered by USB, venue Wi-Fi untouched. See [hardware setup](../robot/SETUP.md).
  - Progress 2026-09-20 02:00: first app -> dog missions on hardware ("go wave at Grandma": found, hello, spoken line, completed; "tell Grandma to plug in her phone": found, spoken via ElevenLabs, Deepgram heard a fragment, reply relayed). A later idle session fired 2 fall check-ins live. Stack: `robot/demo_dog.sh` (app_backend + errand + dog process with missions on :8011). Code gathered under `robot/dog/` (shims at the old paths). Elastic space-time index verified on a loopback node; object detection wired at ~3 fps; 4D viewer served offline.
  - Progress 2026-09-20 00:20: live patrol v0.3 ran for 15-minute sessions greeting 10+ people per session: follow the nearest person with LiDAR-range approach, greet close and centred with a trick rotation, no re-greeting (per-person cooldown + spot memory), fall check-in, bandit exploration over an occupancy map, optional local-VLM proposals with deterministic guardrails, voice commands, streamed live view with boxes and the LiDAR map at `http://127.0.0.1:8011/`. Perception 14 fps (the stream's rate) at ~40 ms latency after the pipeline/diag work. Reports under `output/hardware/`. Open: end-to-end phone -> errand -> body -> dog on hardware; GX10 unreachable (tailnet); Graphiti ingestion of sightings.
  - Progress 2026-09-19 22:52: `robot/go2_patrol_greet.py` patrolled and greeted 7 people in 100 s (firmware Hello + host speech, per-person cooldown; report `output/hardware/2026-09-19-patrol-greet-1.json`) until the hotspot link dropped and the stale-telemetry stop fired. `robot/go2_smart_patrol.py` now supplies the motion: LiDAR voxel-map sector ranges plus an odometry stall detector drive cruise/blocked/backoff/homing modes, verified against a simulated dog (`robot/tests/test_go2_patrol_greet_runtime.py`). Physical verification of the LiDAR ranges and stall backoff is pending the next link; the link-loss stop remains software-only.
  - Done when: a nearby operator can reliably stop the robot and one deliberately slow, bounded physical route completes with measured pose and execution evidence.

- [ ] TASK-008: Persist sign-in server-side (MongoDB was proposed) so a family member's phone survives reinstall, and support family members beyond the fixed `zach`/`ellis` household.
  - Requirement or context: REQ-012, DEC-008, DEC-011. The app-user/dog-user split is gone (DEC-011); sign-in now just picks a family member from `app_backend`'s hardcoded `HOUSEHOLD`, stored on the device (`ProfileStore`).
  - Done when: sign-in writes to the shared backend and survives reinstall; adding a family member is a config change, not a code change on both the app and `app_backend`.
  - Dependency or blocker: the backend owner's requirements for account storage and API shape, and which service owns it. No MongoDB is installed locally; the swift mock backend has no MongoDB driver.

## Done

- [x] Corrected native routing for the exact polite demo request; question punctuation no longer suppresses an explicit resident delivery. Added a distance-only movement guard for unsupported footstep requests. Mac-based recording script and the unconnected lost-phone follow-up are documented in [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

- [x] Adversarial control review: typed stop bypasses planning, cancelled plans cannot replace a new request,
  manual simulation holds between missions, and navigation/audio receipts describe actual outcomes.
  Native iOS buttons verified reminder delivery, exploration, Pause, conversation, and fresh delivery after Pause;
  five repeated simulator deliveries plus cancellation/recovery passed. See [control review](CONTROL_REVIEW.md).

- [x] Recovered simulation planner after the cloud reservation cap produced HTTP 503. The user raised the total allowance to $50 ($49 shared ledger plus the existing separate $1 probe); prior usage is preserved. Fixed actionable availability errors, stopped automatic retries for configuration/budget failures, and removed the misleading Ready label. Live exploration again found the resident and completed spoken playback.

- [x] TASK-015: Live Zach → Annie → Janine story completed twice (REQ-014; [measurements](../robot/simulation/STORY_DEMO.md)): model-selected walking, camera person detection, Zach's reminder played, then a Graphiti-grounded phone answer played. The guarded repeat took 21.779 s / 11.606 s, with 7 model calls and 1.827 s median provider latency. Janine's reply is demo-actor text; memory includes real inferred setup and robot-camera captures.

- [x] TASK-013: Added interactive camera controls, an operator workspace, and DimOS-derived raycast LiDAR, measured-trail and planned-route layers (REQ-013). Browser-verified orbit/pan/zoom/reset and layer acknowledgement; toggling layers changes operator frames while robot-camera frames remain byte-identical.

- [x] TASK-001: Recorded SDK launch evidence and platform/asset blockers; direct MuJoCo and trained Go1-surrogate motion run locally. Full DimOS integration remains separate.
- [x] TASK-003: HTTP body adapter publishes simulated maps/poses and forwards app commands with execution receipts.
- [x] Added deterministic furnished scene batches, realistic cached assets, measured pacing, and verified Blender export.
- [x] Added trained-policy waypoint/patrol movement and robot-camera rendering; no kinematic pose teleporting.

- [x] TASK-002: Integrated the split app service, phone-friendly dashboard, and optional advisory provider. 47 automated checks passed; browser verified bed exemption, reassurance, timeout, acknowledgement, queued message, memory hit/miss, and mobile layout.
- [x] TASK-010: Implemented app_backend's side of the family message relay (`POST /api/messages`, `GET /api/runs/{run_id}`, `GET /api/thread`, `WS /ws/family`, `POST /internal/events`) per `contract/family_messages.md`, plus a mock-mode dispatch path and connectivity/exercise scripts. 86 automated checks passed; both real dispatch outcomes (success and robot-unreachable, ~0.8s to resolve) and the mock-mode full sequence were run and verified against a live server, not just unit-tested. `robot_backend`'s side remains TASK-009.
- [x] TASK-011: Household records (app users, dog users, messages, reminders, reminder history, emergencies) persisted to MongoDB with an in-process fallback, plus the three inbound routes robot_backend reports through: the response to a message, compliance with a reminder, and an emergency (REQ-017, DEC-013). 86 automated checks passed, including two against a real mongod; all six collections were round-tripped through a running server and read back out of the database directly. Nothing writes to the inbound routes in production until TASK-009 lands.

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
