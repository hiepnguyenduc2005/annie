# Annie TODO

Use this file for actionable work. Each task needs a clear completion condition;
link to specification IDs when requirements exist. Move tasks between sections
as work progresses and record a reason for blocked work.

## Now

- [ ] TASK-006: Complete and measure live image/audio inference behind the GX10-compatible service. Keep stale perception out of incident rules; distinguish successful media interpretation from HTTP acceptance.

## Next

- [ ] TASK-004: Connect and evaluate GX10 local image inference. Done when rendered frames produce valid measured observations and the scenario matrix distinguishes VLM results from ground truth.
- [ ] TASK-005: Integrate actual speech, messaging, and Elastic adapters. Done when provisioned services acknowledge execution and privacy boundaries are verified with synthetic data.
- [ ] TASK-006: Implement `robot_backend`'s side of `contract/family_messages.md` (`POST /dispatch`, `POST /internal/events` calls). Done when a real navigate/speak/listen/recall/speak run on the GX10 posts live events into `app_backend` and completes without `ANNIE_FAMILY_MOCK_ROBOT`.

## Blocked

- [ ] TASK-007: Physical Go2 patrol within the operator's requested five-metre boundary (REQ-001; HW-04–HW-06).
  - Verified: V3 authentication, Wi-Fi AP, 720p camera, measured pose/battery, LiDAR, firmware 1.1.15, and enabled obstacle avoidance. See [hardware setup](../robot/SETUP.md).
  - Blockers: operator has no physical controller; independent stop/recovery, moving-stop and link-loss behavior, physical map alignment, and enforced boundary are unverified. A stop acknowledgment at rest and an offline driver fix do not satisfy these checks.
  - Done when: a nearby operator can reliably stop the robot and one deliberately slow, bounded physical route completes with measured pose and execution evidence.

- [ ] TASK-008: Persist profiles server-side (MongoDB was proposed) so the app and dog profiles, their kinds, and which was created first survive across devices.
  - Requirement or context: REQ-012, DEC-008. The SwiftUI registration screens already exist and store profiles on the device.
  - Done when: registration writes to the shared backend; the create-first rules are decided and tested; the app reads profiles back after reinstall.
  - Dependency or blocker: the backend owner's requirements for profile storage and API shape, and which service owns it. No MongoDB is installed locally; the swift mock backend has no MongoDB driver.

## Done

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
