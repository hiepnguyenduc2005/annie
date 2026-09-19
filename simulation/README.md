# Simulation smoke harness

`simulation/smoke.py` is a reproducible physics-only smoke test built on
direct MuJoCo bindings. It loads a local MJCF model, steps it for N steps with
controls held at zero (unactuated), checks the state stays finite and the free
base stays above the floor, detects MuJoCo numerical warnings
(`badqpos`/`badqvel`/`badqacc`), and writes periodic rendered frames plus a
JSON result.

Scope: this harness is a **physics stability smoke test**. It is NOT a gait
test, NOT a policy test, and NOT a DimOS integration test. It never imports
DimOS, never downloads assets, and never opens network connections.

## What it checks

- `--steps > 0` is enforced; `--steps 0` or negative exits 1 before any
  physics runs.
- Model state stays finite throughout (`qpos`, `qvel`, and simulation time).
- The free base (when present) never falls below `z = -1.0`.
- No MuJoCo numerical warnings fire (`badqpos`, `badqvel`, `badqacc`).
- Exit code 0 = all checks passed for the full requested step count.
- Exit code 1 = invalid arguments, model load failure, or divergence.

## Install (separate venv, minimal deps)

The harness needs `mujoco` (>= 3.x for `mujoco.Renderer`), `numpy`, and
`Pillow` for PNG output. A standalone venv, separate from the repo main
environment:

    python3 -m venv /tmp/sim-smoke-venv
    /tmp/sim-smoke-venv/bin/pip install mujoco numpy Pillow

## Supplied model provenance (pinned)

The Go2 scene used for acceptance is a sparse official clone of
`google-deepmind/mujoco_menagerie`, checked out at:

    commit 8161bba264d7fa7c99ca301e91e7fb44737676ad (2026-09-04)
    model: .cache/menagerie/unitree_go2/scene.xml

Sparse clone + pinned checkout:

    git clone --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie.git .cache/menagerie
    git -C .cache/menagerie sparse-checkout set unitree_go2
    git -C .cache/menagerie checkout 8161bba264d7fa7c99ca301e91e7fb44737676ad

Verify the checked-out commit at any time:

    git -C .cache/menagerie rev-parse HEAD

The scene references its meshes by relative path, so the harness uses
`MjModel.from_xml_path` (not `from_xml_string`) to load it. String loading
breaks relative asset resolution and is why the earlier draft failed on this
model.

## Reproduce (exact commands)

From the repo root, using the existing sim venv (mujoco 3.13.0, numpy 2.5.3,
Pillow installed):

    # 1. Built-in minimal drop test, 200 steps
    .cache/dimos/.venv/bin/python simulation/smoke.py \
      --steps 200 \
      --render output/simulation/reproducible/minimal \
      --json output/simulation/reproducible/minimal_result.json

    # 2. Real Go2 scene (unactuated, keyframe-initialized), 500 steps
    .cache/dimos/.venv/bin/python simulation/smoke.py \
      --model .cache/menagerie/unitree_go2/scene.xml \
      --steps 500 \
      --render output/simulation/reproducible/go2 \
      --json output/simulation/reproducible/go2_result.json

Expected: both exit 0 with `"ok": true` in the JSON output. The Go2 starts
from its `home` keyframe (base z 0.27) and settles to z ~ 0.094 after 1.0
simulated seconds, the expected passive-drop behavior for an unactuated
quadruped on its floor plane.

## Recorded acceptance run (2026-09-19)

| Run | Steps | Result | Base z (start -> end) | Wall |
|-----|-------|--------|----------------------|------|
| minimal | 200 | ok | 1.000 -> 0.211 | 0.052s |
| go2 | 500 | ok | 0.270 -> 0.094 | 0.441s |

`--steps 0` correctly exits 1 with an argument error before any physics runs.

Frames are written to `output/simulation/reproducible/{minimal,go2}/` as PNGs
(portable pixmaps as a fallback if Pillow is absent).
