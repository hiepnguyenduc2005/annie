# Stair traversal probe

Measured MuJoCo stair-traversal capability of the cached DimOS Go1 ONNX
policy. The probe composes the pinned Go1 model with a supported ground
floor, three ascending steps, and an upper landing, then drives the policy at
200 Hz physics / 50 Hz policy like the flat-floor path. The base pose is never
written after the initial placement: displacement is physical, state changes
only through `data.ctrl`. Contact surfaces are static box slabs, treads 0.30 m
wide 1.2 m, friction 1.0.

Success requires all four foot sites sustained (1 s) at the far-side
elevation with the base upright, then a zero-command stop. Falling, MuJoCo
warnings, and non-finite states abort the run and count as failure. Height
is read from `qpos`, feet from the FR/FL/RR/RL site frames.

Reproduce from the repository root:

    PYTHONPATH=. .cache/dimos/.venv/bin/python robot/simulation/stairs_probe.py

Validated by `robot/simulation/tests/test_stairs_probe.py`.

## Measured results, 2026-09-19

Speed sweep at three step rises, 30 s per run, one direction at a time.
Success rates are per scenario over the swept speeds
(0.25, 0.35, 0.45, 0.5, 0.6 m/s):

| Scenario | Rise | Best speed | Outcome | Result |
| --- | --- | --- | --- | --- |
| Ascent | 5 cm | 0.5 m/s | success, +2.15 m, feet on landing | PASS (1/5 speeds) |
| Ascent | 10 cm | none | jams against first step at any swept speed | FAIL (0/5) |
| Ascent | 17 cm | none | falls, base height to ~0.015 m | FAIL (0/5) |
| Descent | 5 cm | 0.25 m/s | reaches floor, feet at ground level | PASS (1/5) |
| Descent | 10 cm | 0.25 m/s | reaches floor, feet at ground level | PASS (1/5) |
| Descent | 17 cm | none | falls partway down | FAIL (0/5) |

Timing on passing runs: 5 cm ascent 4.86 s, 5 cm descent 12.51 s,
10 cm descent 11.04 s (first sustained reach after start).

Baseline flat-floor checks in the same session: command 0.4 m/s walks 7.5 m
in 20 s; command 0.2 m/s moves only 0.3 m in 20 s. Momentum-assisted
schedules (0.6 m/s approach for 3-5 s, then 0.4-0.5 m/s) did not change the
10 cm or 17 cm ascent outcomes.

## Interpretation

The cached policy is flat-terrain-only. It cannot track low speed commands
(0.15-0.25 m/s stalls on flat ground), needs roughly 0.5 m/s to climb even a
5 cm step, and cannot climb 10 cm or 17 cm rises at any swept speed. Descents
work up to 10 cm at 0.25 m/s but fail at 17 cm. This is a capability limit of
the policy observation/action pair (48-dim proprioceptive input, no terrain
estimator), not a stair-geometry artifact: the same harness passes on 5 cm.

No ready-to-use stairs/rough-terrain Go1 or Go2 checkpoint was found matching
this policy's proprioceptive-only contract with demonstrated stair
performance (searched unitree_rl_gym, robot-parkour, walk-these-ways;
2026-09-19). Training a terrain-curriculum policy or validating a
depth-input parkour checkpoint are the bounded next steps; both are outside
the no-training, no-new-model scope under which these numbers were recorded.

House stair geometry in `robot/simulation/house.py` (18 risers, 16.67 cm,
tread 30 cm) remains untraversable by this policy and is marked
`traversal_verified: false` in its manifest record. Stair geometry there is
structural scenery, not a supported navigation target.
