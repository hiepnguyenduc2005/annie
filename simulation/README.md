# Annie simulation and live viewer

## Live viewer on localhost

The viewer renders the real Go2 MuJoCo model at 640x480, targeting ten frames
per second. Open [localhost:8766](http://127.0.0.1:8766/) while it is running.
It starts paused and supports play/pause, one physics step, reset, and
front/side/top cameras. The optional joint-pose hold uses a simple PD controller
(`kp=40`, `kd=2`) and bounded motor torques; it is not autonomous walking.
Without hold, zero motor torque lets the robot settle under gravity.

From the repository root, using this checkout's existing simulation environment:

```sh
.cache/dimos/.venv/bin/python simulation/viewer.py \
  --model .cache/menagerie/unitree_go2/scene.xml --port 8766
```

For a fresh checkout, create the minimal environment and clone the pinned model
using the provenance commands below, then substitute
`.cache/sim-venv/bin/python` for `.cache/dimos/.venv/bin/python` in every run
command. DimOS is not required for the viewer, scene factory, or smoke harness.

```sh
uv venv .cache/sim-venv --python 3.12
uv pip install --python .cache/sim-venv/bin/python -r simulation/requirements.txt
```

The server binds only to loopback, validates Host and control origins, and
serves only its own web assets. `GET /state` exposes live telemetry;
`GET /frame.jpg` returns the latest JPEG; `POST /control` queues a validated
control. Each page shares the same simulator. Closing a page leaves the server
running; Ctrl-C in the server terminal stops it.

Timing is real-clock: play paces simulated time to the selected speed factor
(0.25, 0.5, 1, or 2 times wall time), and `/state` reports the measured
achieved speed. If the process stalls, catch-up is bounded to half a wall
second of simulated time; beyond that, wall time is dropped and reported as
`dropped_wall_seconds`. Furnished scenes start with joint-pose hold enabled;
the bare Go2 model starts passive. Scenes auto-pause when their planned
`scenario_duration_s` elapses in simulated time.

Physics and rendering run on the main thread for macOS OpenGL compatibility.
MuJoCo numerical warnings or non-finite states pause physics and latch a visible
error until reset. Reset pauses time and retains the hold preference. Rendering
errors are reported separately. This viewer is currently separate from Annie's
API, incident policy, SDK navigation, and vision inference.

Verified on 2026-09-19: live JPEG rendering and browser controls, single-step
time, camera changes, hold mode, reset, malformed controls, foreign-origin
rejection, and recovery after an injected invalid motor-control value.

## Furnished scene factory

`simulation/scenes.py` deterministically generates furnished home MJCF scenes
around the pinned Go2 model. The default run produces 24 scenes across 6
categories (`safe_bed`, `floor_lying`, `seated`, `standing`, `occluded`,
`empty`) from a fixed seed (default 2026); rerunning with the same seed and
count reproduces the same scenes. Per-scene variation (room size, lighting,
placement, colors, robot home position) derives from `seed + index * 1009`.
Each manifest record carries authored ground truth (resident presence, posture,
support surface, object positions, robot home) labeled
`authored_simulator_labels_not_vlm_output`.

Scenes embed a planned observation schedule (settle, observation window,
planned check-in/review phases, with a `scenario_duration_s` of 120 s for
`floor_lying` and 300 s otherwise). These are labeled
`scenario_schedule_not_executed_actions`: the viewer can show the current phase
from this timeline metadata, but the simulator executes no voice, VLM,
notification, or resident animation, and these schedules do not drive the
application's separate incident-policy timers.

Optional textures come from a prepared asset cache (below). With the cache
present, chair and table render as textured Poly Haven meshes (non-colliding
visuals over stable primitive collision proxies) and non-seated residents use a
textured rigid DimOS person scan: a fixed pose, never animated. Without the
cache, scenes fall back to procedural geometry and mannequins. Each manifest
records which mode was used in `visual_quality`.

```sh
# 24 furnished scenes, 6 categories, seed 2026 (default count and seed)
PY=.cache/dimos/.venv/bin/python   # or .cache/sim-venv/bin/python (fresh checkout)
$PY simulation/scenes.py \
  --assets .cache/menagerie/unitree_go2 \
  --output .data/simulation/scenes

# Larger deterministic batches: the CLI accepts up to 200 scenes per run
$PY simulation/scenes.py \
  --assets .cache/menagerie/unitree_go2 --output .data/simulation/scenes \
  --count 200 --seed 2026
```

The viewer UI can also generate batches on the fly from the loaded catalog; GUI
generation is capped at 96 scenes per batch (the CLI allows 200). Generation
writes each XML atomically and refuses to overwrite scene files it did not
produce.

Run the viewer against a generated catalog:

```sh
$PY simulation/viewer.py \
  --model .cache/menagerie/unitree_go2/scene.xml --port 8766 \
  --scenes .data/simulation/scenes/manifest.json
```

The viewer loads the catalog's first scene, serves `GET /scenes` metadata, and
accepts `scene` and `generate` controls on `POST /control`.

### Optional textured assets (network)

`simulation/assets.py` downloads CC0 Poly Haven assets (chair, table, wood
floor) and reuses the existing DimOS person scan into ignored
`.cache/simulation-assets/`, with a 200 MiB download ceiling and a manifest of
source URLs, hashes, and derived files. It needs `trimesh` beyond the base
runtime:

```sh
uv pip install --python .cache/sim-venv/bin/python -r simulation/requirements-assets.txt
.cache/sim-venv/bin/python simulation/assets.py
```

Scene generation itself never uses the network; it only reads this prepared
cache. See [ASSETS.md](ASSETS.md) for provenance details and [BLENDER.md](BLENDER.md)
for a separate, verified Blender-to-MuJoCo export path.

## Physics smoke harness

`simulation/smoke.py` is a reproducible physics-only smoke test built on
direct MuJoCo bindings. It loads a local MJCF model, steps it for N steps with
controls held at zero (unactuated), checks the state stays finite and the free
base stays above the abort height, detects MuJoCo numerical warnings
(`badqpos`/`badqvel`/`badqacc`), and writes periodic rendered frames plus a
JSON result.

Scope: this harness is a **physics stability smoke test**. It is NOT a gait
test, NOT a policy test, and NOT a DimOS integration test. It never imports
DimOS, never downloads assets, and never opens network connections.

### What it checks

- `--steps > 0` is enforced; `--steps 0` or negative exits 1 before any
  physics runs.
- Model state stays finite throughout (`qpos`, `qvel`, and simulation time).
- The free base (when present) never falls below `z = -1.0`.
- No MuJoCo numerical warnings fire (`badqpos`, `badqvel`, `badqacc`).
- Exit code 0 = all checks passed for the full requested step count.
- Exit code 1 = invalid arguments, model load failure, or divergence.

The same pinned simulation environment runs the viewer, scene factory, and
smoke harness.

## Supplied model provenance (pinned)

The Go2 scene used for acceptance is a sparse official clone of
`google-deepmind/mujoco_menagerie`, checked out at:

```
commit 8161bba264d7fa7c99ca301e91e7fb44737676ad (2026-09-04)
model: .cache/menagerie/unitree_go2/scene.xml
```

Sparse clone + pinned checkout:

```sh
git clone --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie.git .cache/menagerie
git -C .cache/menagerie sparse-checkout set unitree_go2
git -C .cache/menagerie checkout 8161bba264d7fa7c99ca301e91e7fb44737676ad
```

Verify the checked-out commit at any time:

```sh
git -C .cache/menagerie rev-parse HEAD
```

The upstream model and meshes retain their [Unitree license](https://github.com/google-deepmind/mujoco_menagerie/blob/8161bba264d7fa7c99ca301e91e7fb44737676ad/unitree_go2/LICENSE).
Assets remain in the ignored cache rather than being copied into Annie's source.

The scene references its meshes by relative path, so the viewer and harness use
`MjModel.from_xml_path` (not `from_xml_string`) to load it. String loading
breaks relative asset resolution.

## Reproduce (exact commands)

From the repo root, using the existing sim venv (mujoco 3.13.0, numpy 2.5.3,
Pillow installed):

```sh
# 1. Built-in minimal drop test, 200 steps
PY=.cache/dimos/.venv/bin/python
$PY simulation/smoke.py \
  --steps 200 \
  --render output/simulation/reproducible/minimal \
  --json output/simulation/reproducible/minimal_result.json

# 2. Real Go2 scene (unactuated, keyframe-initialized), 500 steps
$PY simulation/smoke.py \
  --model .cache/menagerie/unitree_go2/scene.xml \
  --steps 500 \
  --render output/simulation/reproducible/go2 \
  --json output/simulation/reproducible/go2_result.json
```

Expected: both exit 0 with `"ok": true` in the JSON output. The Go2 starts
from its `home` keyframe (base z 0.27) and settles to z ~ 0.094 after 1.0
simulated seconds, the expected passive-drop behavior for an unactuated
quadruped on its floor plane.

Recorded acceptance run (2026-09-19):

| Run | Steps | Result | Base z (start -> end) | Wall |
|-----|-------|--------|----------------------|------|
| minimal | 200 | ok | 1.000 -> 0.211 | 0.052s |
| go2 | 500 | ok | 0.270 -> 0.094 | 0.441s |

`--steps 0` correctly exits 1 with an argument error before any physics runs.
Frames are written to `output/simulation/reproducible/{minimal,go2}/` as PNGs
(portable pixmaps as a fallback if Pillow is absent).
