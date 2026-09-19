# Simulation findings — SDK and direct MuJoCo

Verified on 2026-09-19. Source and dependency caches are ignored; local logs and
renders are in `output/simulation/`. The live viewer and scene factory are
versioned under `simulation/`.

## Versions

- macOS 26.5.1, Apple Silicon; Python 3.12.13.
- Imported SDK: PyPI `dimos==0.0.13.post1` in `.cache/dimos/.venv`.
- Inspected SDK source: `dimensionalOS/dimos` commit
  `c1c3cdc9d2ee54ca72259465688395699d7d99a2` in `.cache/dimos`.
  This checkout is newer than the installed package; they are distinct artifacts.
- Direct renderer: MuJoCo 3.13.0, NumPy 2.5.3, Pillow 12.3.0.
- Official Go2 model: `google-deepmind/mujoco_menagerie` commit
  `8161bba264d7fa7c99ca301e91e7fb44737676ad`, sparse `unitree_go2` checkout.

## Verified direct simulation

The original Go2 model loads with 19 position coordinates, 18 velocity
coordinates, and 12 actuators. A 500-step zero-torque run advances one simulated
second with finite state and no numerical warnings. Its base settles from
0.270 m to about 0.094 m. This checks passive physics, not locomotion.

The [live viewer](../simulation/README.md) renders actual MuJoCo frames and
supports play/pause, reset, single-step, camera controls, and optional bounded
PD joint holding. Browser controls, malformed requests, origin checks, numerical
fault detection, and reset recovery were exercised. It is separate from the
DimOS navigation/controller stack and from image inference.

A minimal falling-box smoke check also passed 200 steps. Reproducible commands,
pinned dependencies, and model acquisition are in the simulation README.

## DimOS attempts and observed limits

`dimos --help` and `dimos list` run successfully. Bounded launches of
`dimos --simulation run unitree-go2` progressed as follows:

1. Import failed because `cv2` was absent. Installing
   `opencv-contrib-python==4.14.0.94` resolved it. The checkout deliberately
   disables plain `opencv-python` through an override to avoid clobbering contrib.
2. `unitree-webrtc-connect==2.2.0` and its PortAudio dependency were installed.
   Blueprint imports then passed and the worker pool started.
3. SDK host configuration attempted a privileged multicast route change via
   `sudo route add -net 224.0.0.0/4 -interface lo0`; it failed without a TTY.
   No privileged route change was applied. Subsequent diagnostic launches used
   `PYTEST_VERSION=1`, the SDK's test-only host-configuration bypass. This does
   not establish working production networking.
4. Nine navigation/visualization/mapping modules deployed, but the simulator
   connection failed because `git-lfs` was absent. Git LFS 3.8.0 was installed.
5. The last captured full-stack log, `fullstack_attempt3_after_assets.log`,
   still ends with `mujoco_sim` being an LFS pointer after a pull. **The full
   SDK connection has not been demonstrated.**

Asset handling has two roots: the installed SDK can use a clone under
`~/Library/Application Support/dimos/repo`, while inspected/extracted assets
also exist under `.cache/dimos/data`. The cached `mujoco_sim` extraction contains
Go1/G1 policies and office meshes; `person` contains a textured person model.
Separate extraction is not proof that the full-stack launch resolves the same
paths. Aligning the runtime's archive/data root remains a concrete next check.
A sparse Go1 menagerie checkout was also staged to avoid a full model-library
clone. Existing live servers on ports 8000 and 8766 were kept separate from
these bounded experiments.

Logs: `fullstack_attempt.log`, `fullstack_attempt1_sudo.log`,
`fullstack_attempt2.log`, and `fullstack_attempt3_after_assets.log` in the
ignored `output/simulation/` directory. Simulator subprocess startup, windowed
viewer behavior, ONNX policy execution, shared-memory camera output, and
navigation remain unverified together; further blockers may appear.

## SDK integration seams and fidelity

The inspected legacy `mujoco_process.py` maps `unitree_go2` to `unitree_go1`.
The direct Annie viewer uses the actual Go2 asset. Do not treat these paths as
identical dynamics or controller validation.

The source provides mocap person placement through `PersonPositionController`
and `/person_pose`, plus shared-memory odometry, RGB, depth, and lidar writes.
These are useful adapter seams, not a completed Annie implementation. The
newer `MujocoSimModule` exposes a headless configuration but was not exercised.
Ground-truth scene labels must remain separate from model-derived perception.
