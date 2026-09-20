# Control responsiveness

The command center separates three software measurements:

- `control_hz`: outer behaviour-loop iterations in the last three seconds. The
  field remains compatible with older clients. Greetings, speech, listening,
  inference and firmware settlement can pause this loop.
- `safety_hz`: actual safety-check evaluations, including checks inside those
  waits and nested movement loops. A timer or UI refresh does not count.
- `motion_tx_hz`: velocity requests sent to the transport. These are requests,
  not proof of physical actuation or the robot's onboard controller frequency.

`safety_age_ms` reports time since the last successful check.
`safety_max_gap_ms` includes both the longest recent interval and the current
age, so stopped checks remain visible even after their samples age out.
Diagnostics refresh every 250 ms independently of the behaviour loop. The
existing guarded waits recheck safety at intervals of at most 50 ms plus event
loop scheduling delay; this is not a real-time scheduling guarantee.

Both app Stop and a recognized voice Stop interrupt guarded operations. The
runtime requests neutral movement and StopMove, cancels the action, and holds
until an explicit new command. Voice timing here starts when a recognized command reaches the runtime;
it excludes microphone capture and transcription. Cancelling the awaiting task
does not guarantee that a provider thread or already-started audio playback
has stopped. Software stop still depends on a working link.

Regression checks (no hardware, audio devices, or external model calls):

```sh
.venv/bin/python -m pytest robot/tests/test_control_responsiveness.py -q -s
```

The tests hold speech pending, check that diagnostics keep updating, inject app
and voice Stop, and require neutral commands and a StopMove request within
300 ms. Late speech completion must not resume movement or complete the cancelled
mission. A separate clock-controlled check proves that stale safety checks are
reported as stale rather than healthy. Physical stop latency requires a
separate supervised measurement.
