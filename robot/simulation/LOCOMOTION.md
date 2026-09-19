# Trained simulation locomotion

The live motion path uses a **Unitree Go1 surrogate**, paired with the cached
DimOS Go1 ONNX policy. It produces physical displacement through twelve joint
position actuators in MuJoCo. It never rewrites the free-base pose to simulate
walking. This does not validate a Go2 controller, physical robot, hardware stop,
DimOS navigation stack, or autonomous obstacle avoidance.

## Reproduce

The current cached environment is `.cache/dimos/.venv/bin/python` (Python 3.12,
MuJoCo 3.13.0, NumPy 2.5.3, ONNX Runtime 1.30.0). A viewer environment can install
`robot/simulation/requirements-locomotion.txt`; no GPU is required. CPU inference uses
one intra-op and one inter-op thread.

Required local files:

- `.cache/dimos/data/mujoco_sim/unitree_go1.xml`
- `.cache/dimos/data/mujoco_sim/unitree_go1_policy.onnx`
- `.cache/menagerie_full/unitree_go1/assets/*.stl`

DimOS source revision is `c1c3cdc9d2ee54ca72259465688395699d7d99a2` from
[dimensionalOS/dimos](https://github.com/dimensionalOS/dimos/tree/c1c3cdc9d2ee54ca72259465688395699d7d99a2).
The observation/action layout is adapted from its Apache-2.0
`dimos/simulation/mujoco/policy.py` and `model.py`; the matching MJCF and ONNX
are its downloaded `mujoco_sim` data assets. This records their source and
hashes; it does not infer separate policy-data licensing from the code license.
The meshes are from
[MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie/tree/8161bba264d7fa7c99ca301e91e7fb44737676ad/unitree_go1),
revision `8161bba264d7fa7c99ca301e91e7fb44737676ad`; retain its `unitree_go1/LICENSE`.

| File | SHA-256 |
| --- | --- |
| `unitree_go1.xml` | `97058b2d17ee311cc7eddcd33e87524717533916fb597448b3c8745875c012e0` |
| `unitree_go1_policy.onnx` | `386cde6a1eac679e2f2a313ade2b395d9f26905b151ee3b019c2c4163d49b6f2` |

### Fresh-checkout bootstrap

`robot/simulation/setup_locomotion.py` prepares everything above in one command. It
downloads the pinned DimOS `mujoco_sim` archive (~60 MB; hard 100 MB cap) from
`media.githubusercontent.com` at commit `c1c3cdc9d2ee54ca72259465688395699d7d99a2`
(the URL serves the real gzip blob, not an LFS pointer), extracts ONLY the two
Go1 files by scanning tar members read-only and copying those two by basename,
and verifies both SHA-256 hashes above before accepting them. An archive
already present under `.cache/dimos/data/.lfs/` is reused instead of
re-downloading. It also prepares `.cache/menagerie_full` as a sparse Menagerie
clone (only `unitree_go1`) pinned at `8161bba264d7fa7c99ca301e91e7fb44737676ad`,
using plain git with no global configuration changes; if that directory
already exists at a different commit, the script stops and asks for manual
repair instead of mutating the existing checkout.

Reruns verify the cached files, print `Ready`, and skip: nothing is
re-downloaded or overwritten while the hashes match. A hash mismatch is an
error, never a silent fix. Everything lives in ignored cache; no binaries are
committed.

```sh
uv venv .cache/sim-venv --python 3.12
uv pip install --python .cache/sim-venv/bin/python \
  -r robot/simulation/requirements.txt -r robot/simulation/requirements-locomotion.txt
# Optional textured furnished scenes:
uv pip install --python .cache/sim-venv/bin/python -r robot/simulation/requirements-assets.txt
.cache/sim-venv/bin/python robot/simulation/assets.py

.cache/sim-venv/bin/python robot/simulation/setup_locomotion.py
```

Generate scenes and run the live viewer with walking enabled:

```sh
PY=.cache/sim-venv/bin/python
$PY robot/simulation/scenes.py --assets .cache/menagerie/unitree_go2 \
  --output .data/simulation/scenes
$PY robot/simulation/viewer.py --model .cache/menagerie/unitree_go2/scene.xml \
  --port 8766 --scenes .data/simulation/scenes/manifest.json --locomotion
```

Run from the repository root:

```sh
.cache/dimos/.venv/bin/python robot/simulation/tests/test_locomotion.py
```

This check composes a furnished empty scene (seed 2026), runs 20 simulated
seconds, checks finite state, all MuJoCo warnings, upright orientation and
height, positive forward displacement, turning, bounded stopping, and rejected
non-finite/out-of-range commands. It writes exact results to
`.cache/locomotion/validation.json`.

Measured on 2026-09-19, each phase four simulated seconds:

| Phase | Body velocity request (m/s, m/s, rad/s) | Measured result |
| --- | --- | --- |
| Stand | 0, 0, 0 | Settled height 0.293 m |
| Forward | 0.4, 0, 0 | World x +1.448 m; final-second mean planar speed 0.397 m/s |
| Stop | 0, 0, 0 | Net planar displacement 0.0104 m; final-second speed 0.000215 m/s |
| Turn | 0, 0, 0.5 | Yaw +0.682 rad; command tracking is imperfect |
| Stop after turn | 0, 0, 0 | Final-second speed 0.000043 m/s |

Minimum base height across all phases was 0.283 m. These are bounded flat-floor
results, not a guarantee over furniture collisions or arbitrary commands.

Separate waypoint-mission run (2026-09-19), live user session with
`--locomotion` on a furnished scene catalog: a full patrol visiting the
authored waypoints completed in 22 s wall time with zero furniture contacts
(MuJoCo contacts limited to the floor). This exercises the viewer's A*
planner over the **authored** scene collision map. It is not SDK SLAM, not
perceived obstacles, and not a Go2 hardware result.


## Viewer integration API

```python
from robot.simulation.locomotion import prepare_locomotion_model, LocomotionController

path = prepare_locomotion_model(scene_path)
model = mujoco.MjModel.from_xml_path(str(path))
data = mujoco.MjData(model)
mujoco.mj_resetDataKeyframe(model, data, 0)
mujoco.mj_forward(model, data)
controller = LocomotionController(model, data)  # optional policy_path=Path(...)

# Every physics step; vx/vy are robot-body axes, wz is yaw radians/second.
controller.apply(vx, vy, wz)
mujoco.mj_step(model, data)
```

`prepare_locomotion_model(Path)->Path` writes content-addressed ignored XML.
It keeps Annie scene `env_` geometry, textures, lights and camera settings;
replaces the Go2 robot with the complete matching Go1 model; preserves home
x/y/quaternion; and uses the policy's Go1 home height/joints. The compiler must
use **radians**: the source Go1 file relies on the parent scene for that setting.
Only trusted locally generated Annie scenes are supported. The environment
meshes remain visual-only where the generator marked them that way; their
existing furniture collision proxies remain. Go1 collision geoms are group 3,
visual meshes group 2, consistent with the viewer hiding group 3.

`LocomotionController(model,data,policy_path=None)` validates joint dimensions,
actuator order, position gains and sensors. It sets the model timestep to 0.005
seconds. `apply(vx,vy,wz)` runs the policy every fourth step (50 Hz), scales
12 output actions by 0.5 and adds the trained home angles. Accepted absolute
command limits are 0.6 m/s forward, 0.3 m/s lateral and 1.0 rad/s yaw. Commands
must be finite. These are conservative demo bounds, not calibrated performance.
No global MuJoCo callback is installed.

On reset, first reset MuJoCo and call `mj_forward`, then `controller.reset()` to
clear action history and restore initial controls. Do not apply separate pose
holding or overwrite `data.ctrl` while this controller is active. Zero velocity
means active standing with settling time; pausing physics is a distinct UI action.
Policy/physics faults must pause the simulation and require reset.

`controller.state()` returns model label, policy hash, requested body velocity,
physics/policy periods and `hardware_connected=False`. Robot base body is
**`trunk`**, not the Go2 name `base`; use `controller.base_id` for pose/camera
tracking. `controller.imu_id` identifies the policy IMU site. Measured pose and
velocity must still come from MuJoCo data. A heading/waypoint controller should
close its loop on actual pose because requested yaw is not perfectly tracked.
