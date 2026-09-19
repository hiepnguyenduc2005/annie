# Patrol supervision verification — 2026-09-19

This work adds offline physical-adapter admission and tests the simulator
bridge's asynchronous command boundary. It did not connect to or actuate the
physical Go2. The hardware task reported that the robot was powered off.
The simulator owner committed the production bridge fixes as `10023d2` before
this regression suite was committed. The hardware owner independently reran
the 44 pure supervisor checks and reviewed the gate; all passed.

## Results

| Check | Result | Evidence boundary |
| --- | --- | --- |
| `robot/patrol/tests/test_supervisor.py` | 44 passed | Synthetic values; no transport or model calls. |
| `robot/patrol/tests/test_bridge_supervision.py` | 11 passed | Actual `Bridge.think`; mocked HTTP/model responses. |
| App backend plus physical supervisor tests | 189 passed | Existing local app tests plus the 44 supervisor cases; one upstream deprecation warning. |
| Contract schema export check | Passed | No wire-schema changes in this work. |
| Family frontend JavaScript syntax | Passed | `node --check robot/frontend/app.js`. |
| MuJoCo stop/resume | Passed | `safe_bed-000`, matched Go1 policy, measured arrival; no renderer/hardware. |
| MuJoCo command-ID deduplication and queue bounds | Passed | Existing real-physics navigation test, not physical Go2. |

Commands run from the repository root:

```sh
.venv/bin/python -m pytest robot/patrol/tests -q
.venv/bin/python -m pytest robot/app_backend/tests robot/patrol/tests/test_supervisor.py -q
.venv/bin/python robot/contract/export_schemas.py --check
node --check robot/frontend/app.js
.cache/dimos/.venv/bin/python robot/simulation/tests/test_navigation.py \
  NavigationPhysicsTests.test_stop_settles_then_resume_completes \
  NavigationPhysicsTests.test_command_ids_idempotent_and_queue_bounded
```

An initial navigation invocation using the root `.venv` failed because that
environment lacks MuJoCo. The two relevant physics checks were then run with
the documented cached DimOS Python environment and passed. The full navigation
scenario matrix was not rerun by this task.

## Bugs reproduced and handed to the simulator owner

1. An accepted app motion command with viewer navigation still idle allowed
   another `goto`. The owner added a fresh outstanding-command check and
   retained the submitted command until its identified terminal receipt.
2. A slow outstanding-command lookup could outlive the original frame's
   five-second validity after the execution gate had already approved it.
3. Pausing the goal during that lookup could also leave an earlier enabled
   snapshot eligible for submission.

The owner moved the command lookup before the status/viewer reads and ran
the final expiry/goal checks immediately before submission. All three
reproductions now reject new motion. The suite also preserves a successful
fresh action as a positive control, and checks model timeout, already-expired
plans with a fresh current camera, map/goal changes, stale camera and active
navigation. Test requests must reach the model path, preventing a vacuous
pass caused by an earlier unrelated failure.

## Remaining physical acceptance

Importing this package, passing these tests, connecting the camera, and
acknowledging a stop while stationary do not satisfy the physical gates.
The adapter still needs integration and controlled verification of moving
stop, operator recovery, onboard loss-link/host-power-loss stopping, actual
boundary alignment and obstacle behavior. Read [the unmet start conditions](README.md#operator-start-conditions).

Host-side validation still has transport delay between admission and execution.
A body adapter must bind the dispatched command to its map/session/goal and
enforce current interlocks when executing it. A Python gate cannot guarantee
containment after loss of its host, network or underlying controller.
