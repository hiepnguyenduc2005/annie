# Simulation findings — DimOS SDK vs direct MuJoCo

Date: 2026-09-19. All work read-only wrt tracked files; artifacts live in
`.cache/` (ignored) and `output/simulation/`.

## Versions and provenance

Two DimOS artifacts are in play and must not be conflated:

- **Installed SDK (importable):** `dimos` 0.0.13.post1 from PyPI, living in
  site-packages of the sim venv `.cache/dimos/.venv`.
- **Inspected source (not imported for these checks):** git clone of
  `dimensionalOS/dimos` at HEAD `c1c3cdc9d2ee54ca72259465688395699d7d99a2`
  (2026-09-19, "Merge release/0.0.14 back to main") in `.cache/dimos`.

Environment: macOS 26.5.1 (Apple Silicon), Python 3.12.13, uv-managed venv,
MuJoCo **3.13.0**, Pillow 12.3.0. 282 packages, ~3.3 GB, no CUDA. Docker
daemon not running and not needed. `git-lfs` absent on host.

## What actually ran (verified)

### 1. Direct MuJoCo physics — REAL, headless, macOS

Minimal free-body drop, 200 steps via `mujoco.mj_step`: base z 1.0 to 0.211
with rebound, no divergence, 1 ms wall time. Harness:
`uv pip install --python .venv/bin/python 'dimos[base,sim]'` then
`python simulation/smoke.py --steps N [--model x.xml] [--render dir]`.

This exercises **physics only**. A sphere drop proves no robot model, no
controller, no DimOS bridge. It is not a robot-sim result.

### 2. Official Unitree Go2 MJCF (mujoco_menagerie) — see output dir

Partial clone `git clone --filter=blob:none --sparse` of
`google-deepmind/mujoco_menagerie` into `.cache/menagerie`, sparse checkout
of `unitree_go2` + LICENSE only (avoids the ~1 GB full tree). `scene.xml`
loaded and stepped headless with the SDK venv MuJoCo 3.13.0; frames and a
result JSON are in `output/simulation/`. This is still **direct MuJoCo on
the vendor model** — no DimOS involvement — but it is the actual Go2
description, unlike the DimOS sim stack (see caveat below).

## Full DimOS CLI attempts - observed progression (all logs in output/simulation/)

Three bounded, finite launch attempts of
`dimos --simulation run unitree-go2` (PyPI SDK in the venv), each killed
cleanly after 20-30 s:

1. `fullstack_attempt.log` - failed at import:
   `ModuleNotFoundError: No module named cv2`. Root cause found: the
   checkout pyproject `override-dependencies` forces `opencv-python` to
   `sys_platform == never` (contrib-clobber guard), so uv silently
   installs nothing for it. Fix within authorized budget: installed
   `opencv-contrib-python 4.14.0.94` (49.9 MB), matching the project own
   core dep spec.
2. `fullstack_attempt1_sudo.log` - after also installing
   `unitree-webrtc-connect 2.2.0` (dep #2; needed a Homebrew `portaudio`
   bottle first, 521 KB): blueprint imports all pass, 10-worker pool
   starts, then the run dies on a privileged host change -
   `sudo route add -net 224.0.0.0/4 -interface lo0` (LCM multicast route)
   returns non-zero without a TTY. Not applied: that is a system-level
   network change needing explicit approval.
3. `fullstack_attempt2.log` - using the code own documented test bypass
   (`PYTEST_VERSION=1` makes `configure_system` skip host changes; logged:
   "Pytest run detected: skipping system configuration."), the stack went
   further than any prior attempt: "Building the blueprint", worker pool
   started, and 9 modules deployed (RerunWebSocketServer, MovementManager,
   PatrollingModule, WavefrontFrontierExplorer, ReplanningAStarPlanner,
   WebsocketVisModule, CostMapper, VoxelGridMapper, RerunBridgeModule).
   It then reached `MujocoConnection.__init__` and died at
   `get_data("mujoco_sim")` with `RuntimeError: Missing required tools:
   git-lfs` - the previously predicted blocker, now OBSERVED.

Dep budget used: 2 of 3 (opencv-contrib-python, unitree-webrtc-connect).
No orphan processes remained after the bounded kills.

Remaining blockers to a full local sim run, all now concrete:

- `git-lfs` absent (blocks the ~60 MB `mujoco_sim` LFS tarball) - needs a
  Homebrew install, outside the authorized Python-dep budget.
- LCM multicast route (`sudo route add ... lo0`) - privileged; or run
  persistently with the test bypass, untested beyond 30 s.
- After LFS: menagerie self-clone (~1 GB) and `viewer.launch_passive`
  (windowed) in the legacy path remain untested risks.

## Material caveats found in source (clone HEAD above)

- **DimOS "Go2 sim" is a Go1 model.** `dimos/simulation/mujoco/`
  `mujoco_process.py` rewrites `robot_model == "unitree_go2"` to
  `"unitree_go1"` before loading; policies shipped are
  `unitree_go1_policy.onnx` / `unitree_g1_policy.onnx`.
- `MujocoConnection` (subprocess + shared memory) is the Go2 sim path from
  `--simulation mujoco`; it uses `viewer.launch_passive` (windowed) in the
  legacy process. The newer `MujocoSimModule` engine exposes
  `headless: bool = False` config — promising untested headless route.
- Ground-truth hook: mocap `person` body driven by
  `PersonPositionController` subscribing `/person_pose` — clean seam for
  manual ground truth separated from actual VLM perception.
- Shared-memory writes per tick: `write_odom(pos, quat, t)`, `write_video`,
  `write_depth`, `write_lidar` — capture points for synced (x, y, t)+caption.

## Bottom line

Direct MuJoCo on the official Go2 model: works headless on this macOS host
today. Full DimOS sim stack: imports and deploys 9 of its modules on this
host, with the sim connection itself blocked at asset LFS - an observed,
concrete, single-tool gap (install git-lfs, authorize the multicast route
or keep the test bypass) rather than an OS incompatibility. "Go2 sim"
fidelity caveat (Go1 model) must be carried into any spec claims. Nothing
here yet demonstrates the DimOS SDK full sim pipeline end to end.
