# Local simulation assets

Run `.cache/dimos/.venv/bin/python robot/simulation/assets.py` to prepare the optional
textured asset cache. Requires trimesh, NumPy, and Pillow. Scene generation never
uses the network and falls back to procedural geometry when prepared assets are
absent. Downloaded binaries stay in ignored `.cache/simulation-assets/`.

Powered by [Poly Haven](https://polyhaven.com/). The script queries the official
API for actual 1K glTF files and dependencies, with a 200 MiB new-download ceiling:

- [Modern Arm Chair 01](https://polyhaven.com/a/modern_arm_chair_01), CC0.
- [Small Wooden Table 01](https://polyhaven.com/a/small_wooden_table_01), CC0.
- [Wood Floor](https://polyhaven.com/a/wood_floor), CC0 albedo.

The resulting manifest records source URLs, relative cache paths, SHA-256 hashes,
source sizes, derived meshes, textures, and target dimensions. glTF transforms
and UV coordinates are retained while converting to individual OBJ parts. Chair
height is 0.95 m; table height is 0.795 m including its top. The table's simple
collision top remains centered at 0.75 m. Mesh visuals are noncolliding; stable
primitive furniture collision shapes occupy group 3.

The existing DimOS `data/person/jeong_seun_34.obj` and `material_0.png` are reused.
Upstream package: [person.tar.gz](https://media.githubusercontent.com/media/dimensionalOS/dimos/main/data/.lfs/person.tar.gz).
[Upstream integration](https://github.com/dimensionalOS/dimos/blob/main/dimos/simulation/mujoco/model.py)
rotates this Y-up scan by 90 degrees about X. We apply that transform, normalize
height to 1.72 m, and preserve UV coordinates. The asset-specific license has not
been established; it is recorded as unknown rather than inferred from the SDK's
code license. No person asset is redistributed in tracked source.

The resident scan is a fixed rigid pose, including its lying variants, not an
articulated or animated human. Seated scenes retain a procedural mannequin.
Scene `visual_quality` metadata names the assets and fallback used. Ground truth
is authored simulator data; it does not represent perception, calibrated
visibility, medical assessment, or actual human behavior.

Timelines specify repeatable planned observation windows in simulation seconds.
They execute no voice, VLM, notification, or resident animation. These scene
schedules do not change the application's separate incident-policy timers.

## Spatial visualization code

`spatial.py` adapts the batched `mj_multiRay` operation, world-direction transform,
range-validity filtering, and hit-point reconstruction from DimOS
[`_raycast_lidars`](https://github.com/dimensionalOS/dimos/blob/c1c3cdc9d2ee54ca72259465688395699d7d99a2/dimos/simulation/engines/mujoco_engine.py#L485),
commit `c1c3cdc9d2ee54ca72259465688395699d7d99a2`.
Copyright 2025 Dimensional Inc., Apache-2.0; the upstream license is retained in
[licenses/DimOS-Apache-2.0.txt](licenses/DimOS-Apache-2.0.txt).

Annie's changes add a bounded panoramic virtual sensor, exclude the whole robot
subtree using a private model copy, retain a bounded measured trajectory, and
render optional operator-only scan/trail/route overlays. This is direct MuJoCo
sensor simulation, not a claim that the full DimOS or Rerun stack is running.
The scene camera remains independent of the robot's inference camera.
