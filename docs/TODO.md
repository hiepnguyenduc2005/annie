# Annie TODO

Use this file for actionable work. Each task needs a clear completion condition;
link to specification IDs when requirements exist. Move tasks between sections
as work progresses and record a reason for blocked work.

## Now

- [ ] TASK-001: Verify the SDK/simulator on an actual supported path. Done when versioned commands, results, and platform blockers are in `SIMULATION_FINDINGS.md` and match `../simulation/SPEC.md`.

## Next

- [ ] TASK-003: Connect a simulation-only body adapter to the API. Done when real simulator status/pose reaches the app with correct time/map IDs and no physical-robot fallback.
- [ ] TASK-004: Connect and evaluate GX10 local image inference. Done when rendered frames produce valid measured observations and the scenario matrix distinguishes VLM results from ground truth.
- [ ] TASK-005: Integrate actual speech, messaging, and Elastic adapters. Done when provisioned services acknowledge execution and privacy boundaries are verified with synthetic data.
- [ ] TASK-006: Implement `robot_backend`'s side of `contract/family_messages.md` (`POST /dispatch`, `POST /internal/events` calls). Done when a real navigate/speak/listen/recall/speak run on the GX10 posts live events into `app_backend` and completes without `ANNIE_FAMILY_MOCK_ROBOT`.

## Blocked

None recorded.

## Done

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
