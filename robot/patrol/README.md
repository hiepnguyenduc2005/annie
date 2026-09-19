# Physical patrol supervision

This package supplies a typed, offline admission gate for the future physical
adapter. It does not connect to the Go2, run a model, start a patrol, or send a
stop command. The hardware task retains ownership of connection and actuation.
The robot was reported powered off during this work on 2026-09-19.

It implements the prerequisites in [HW-04 through HW-06 and HW-09](../../docs/ACCEPTANCE.md)
and the timestamp, identity and execution distinctions in the
[robot contract](../contract/README.md). Integration into the physical adapter
and a controlled moving rehearsal remain open.

## Operator start conditions

The verified stationary camera, pose, LiDAR and StopMove acknowledgment do not
establish permission to patrol. Keep every unverified field false. Before
autonomous motion, the hardware owner must record:

- A usable physical stop and recovery method with the nearby operator.
- Measured stopping from motion at the configured maximum speed. A StopMove
  acknowledgment while already stationary does not qualify.
- Onboard stopping after loss of the host connection or host power. A host
  watchdog cannot send a stop through a lost network or after the host dies.
- Physical map/odometry alignment and a verified fixed patrol boundary.
  The proposed 5 m total width is represented by a 2.5 m radius centered at
  the recorded origin; it is not yet a measured hardware containment guarantee.
  Set body clearance from the robot's swept body/foot envelope and validate
  pose uncertainty. The default numeric settings are unqualified demo values.
- Obstacle-avoidance behavior in that controlled area. An `enabled=true` flag
  alone does not qualify.
- A conservative measured stopping-distance bound that covers both the moving
  stop and loss-link tests, at the configured maximum speed.

`missing_verifications(Verification())` returns all six unmet requirements as
operator-facing text. These are controlled commissioning checks, separate
from autonomous patrol admission. No flags are set automatically by this module.

## Adapter API

All times are **monotonic seconds from the same host process/session**. Preserve
actual sensor receipt/capture identity; polling old data must not refresh its
timestamp. Convert a frame's wall-clock timestamp to a monotonic timestamp
once when capturing it, with validated synchronization. Do not copy Unix
milliseconds into this API or re-date a slow model result.

1. Construct immutable `Boundary(map_id, session_id, origin_x_m, origin_y_m,
   radius_m)` and `Limits`. Reconnect or relocalization invalidates this frame;
   stop, re-verify and create a new supervisor. Never recenter a boundary as
   the robot walks.
2. Supply `Telemetry` with actual hardware source, session/map identity, pose
   uncertainty, speed, battery, camera/pose/LiDAR times, connection state and
   obstacle readiness. Supply `Verification` only from the controlled checks
   above. Do not substitute simulated poses or optimistic default booleans.
3. Call `arm(sample, verification, now_s=..., goal_revision=...)` only for an
   explicit operator start/resume. It requires fresh evidence and a stopped
   robot; it returns `hold/armed_waiting_for_plan`, which sends no motion.
4. Run `check(...)` independently of model inference at the configured
   supervision interval. Pass the actual enabled flag and current goal revision.
   Missing/stale evidence, low battery, excessive speed, pose jumps, changed
   frame, disabled avoidance, exhausted stopping margin, pause or changed goal
   latch a stop after arming. Clear data alone never releases that latch.
5. Pass a `MotionProposal` to `admit(...)`: canonical command/frame UUIDs,
   session/map identity, goal revision, original frame and plan times, target
   in metres, and speed in metres/second. Only `allow/motion_reserved_once`
   authorizes one adapter dispatch. Reservation occurs **before** dispatch;
   a timeout or missing receipt cannot cause a retry. Use one supervisor and
   one transport owner; this class has no concurrency lock.
6. Supply matched `Receipt` values through `record_receipt(...)`. Accepted or
   executing receipts cannot release a pending command. Completion also needs
   a pose received after that receipt, within arrival tolerance including
   uncertainty, with measured speed below the stopped threshold. Re-submit
   the identified completion receipt with a later pose if the gate reports
   `awaiting_post_completion_pose`. A failed or timed-out command latches a
   stop. An unresolved command cannot be erased by calling `arm`.

`hold` inhibits new motion. `latched_stop` requires the adapter's independent
stop procedure and explicit recovery; it is not an execution receipt and
does not establish that the robot has physically stopped. Keep the latched
state visible to the operator. The adapter must recover unresolved commands
from its own identified execution journal before creating a replacement gate.
Bind each verification record to this unit, connection/session, software
revision and configured speed/limits in the adapter's evidence journal;
changing those conditions requires renewed verification.

The configured boundary reserves body clearance, pose uncertainty, measured
stopping distance and one supervision interval of travel. Both the current
pose and target must fit. A waypoint inside a circle alone does not prove a
curved route stays inside: the adapter must validate its path and continuously
check measured pose. This gate provides no obstacle planner, SLAM, dynamic
collision guarantee or independent hardware emergency stop.

## Model and command timing

The original model frame expires after at most five seconds, even when the
current camera is fresh. The frame must also be captured after the current
arm. A seven-second model answer cannot become a fresh decision by replacing
its timestamp or citing a different frame. Run a faster local model or obtain
a new image-grounded decision; do not weaken the expiry check to make a demo move.

Do not use an idle navigation state as evidence that all commands finished:
an accepted app command may still be waiting to reach the body. The gate
retains a submitted ID until its terminal receipt and measured arrival, and
deduplicates completed command and frame IDs for its bounded session. After
4096 admissions it requires a new reviewed session rather than forgetting IDs.

## Offline verification

```sh
.venv/bin/python -m pytest robot/patrol/tests/test_supervisor.py -q
.venv/bin/python -m pytest robot/patrol/tests/test_bridge_supervision.py -q
```

The supervisor tests use synthetic numeric telemetry and no network. The bridge
tests run the actual simulator `Bridge.think` with mocked HTTP responses. They
exercise delayed inference, timeout, pause, changed goal/map, stale camera,
active movement and an accepted command while navigation still reports idle.
They do not qualify a model's accuracy, full patrol reliability, physical
stopping distance or real hardware behavior.

Measured status and the accepted-command regression are recorded in
[VALIDATION.md](VALIDATION.md).
