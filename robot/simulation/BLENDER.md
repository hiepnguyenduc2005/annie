# Blender scene export for MuJoCo environments

Practical asset bridge from Blender scenes to MuJoCo MJCF. Blender does not
import `.blend` files into MuJoCo directly; this exporter writes meshes and a
static `environment.xml`, and a composer merges that environment into the Go2
MJCF for actual loading.

Verified both paths end-to-end on 2026-09-19 with **Blender 5.2.2 LTS**
(installed via the official `brew install --cask blender`) against the
current official API documentation, and the pinned Go2 MuJoCo model.

## Primary sources

- Blender OBJ export operator `bpy.ops.wm.obj_export` (replaces the 3.x
  `bpy.ops.export_scene.obj`, removed in Blender 4.0):
  https://docs.blender.org/api/current/bpy.ops.wm.html#bpy.ops.wm.obj_export
  - Key parameters used: `export_selected_objects`, `apply_modifiers`,
    `export_triangulated_mesh`, `export_uv=False`, `export_normals`,
  `export_materials=False`, `forward_axis`, `up_axis`. Default axis
  convention is `forward_axis=NEGATIVE_Z`, `up_axis=Y`, which would rotate
  geometry; the exporter pins `Y`/`Z`, verified empirically in Blender 5.2
  to pass Blender world coordinates through unchanged (`NEGATIVE_Y` rotates
  180 deg about Z), matching MuJoCo's Z-up world convention.
- Blender 4.0 Python API change note (legacy OBJ add-on removed):
  https://developer.blender.org/docs/release_notes/4.0/python_api/
- MuJoCo compiler `meshdir` (where the compiler looks for mesh files):
  https://mujoco.readthedocs.io/en/stable/XMLreference.html#compiler-meshdir
- MuJoCo collision semantics: geom-geom collision supports convex geometry
  only; a mesh geom collides via its convex hull:
  https://mujoco.readthedocs.io/en/stable/modeling.html#collision-detection
  (the Overview chapter states only convex geom-geom collisions are
  supported). This is why the exporter emits primitive BOX collision geoms
  from world-space bounding boxes instead of mesh collisions, and marks each
  one as an approximation in the bundle warnings.
- MuJoCo mesh path resolution: absolute `file` values bypass `meshdir`;
  relative values resolve as `meshdir/file`
  (https://mujoco.readthedocs.io/en/stable/XMLreference.html#compiler-meshdir).
  The composer writes absolute paths so the merged file loads from any
  working directory without touching the robot's own `meshdir`; verified
  below by loading the merged model from a different working directory.

## Export (inside Blender)

    blender --background scene.blend --python robot/simulation/blender_export.py -- \
        --output .data/simulation/blender/room

Behavior:

- Visual mesh objects export to `meshes/mesh_<name>.obj`, triangulated,
  modifiers applied, world transforms baked into vertices, Z-up meters kept.
- Objects in a collection named `COLLISION` or named with the prefix `COL_`
  become static BOX geoms sized from their world bounding box, flagged as
  axis-aligned approximations. Other collision shapes (sphere/capsule) are
  not generated; extend the script if needed.
- Visual geoms get `contype="0" conaffinity="0" group="2"` so they render but
  never collide; collision boxes keep default contact settings (default
  friction) so the robot collides with them.
- Materials become flat RGBA colors read from the first Principled BSDF Base
  Color. No textures are exported; texture fidelity is intentionally absent.
- Scene units: the metric system is required; `unit_settings.scale_length`
  is folded into every exported vertex (composed into the world transform)
  and into collision box centers and half-extents, so any metric scale lands
  in true meters. Non-metric scenes (e.g. imperial) are rejected with exit 1.
  Verified in Blender 5.2: a 1 m cube rotated 35 deg about Z produced
  collision half-extents (cos35+sin35)/2 = 0.696364 (exact analytic match),
  a non-uniformly scaled visual cube exported with correct world coordinates
  (x 0.8-1.4, y -0.1-0.3, z 0.15-0.45), and an imperial-unit scene exited 1
  with a refusal message.
- Output bundle (schema below) uses basenames only; the export writes only
  into the configured output directory and accepts no arbitrary asset paths
  at export time. The compose step reads the environment bundle plus the
  robot MJCF (and its included meshes) by explicit user-provided path and
  refuses relative `..` escapes. No network access occurs in either path.

`environmentbundle.json` schema:

```json
{
  "version": 1,
  "units": "m",
  "up_axis": "Z",
  "mjcf": "environment.xml",
  "source": "blender",
  "objects": [
    {"name": "COL_floor", "role": "collision", "type": "box"},
    {"name": "table", "role": "visual", "type": "mesh", "file": "meshes/mesh_table.obj"}
  ],
  "warnings": ["collision geom 'COL_floor' is an axis-aligned bounding-box approximation of the mesh"]
}
```

## Compose (outside Blender, plain Python)

    python robot/simulation/blender_export.py compose \
        --environment .data/simulation/blender/room/environment.xml \
        --robot .cache/menagerie/unitree_go2/scene.xml \
        --output .data/simulation/blender/room/merged.xml

The robot file is not modified. The merged output inlines the robot's
`<include>` elements (so the output is self-contained), copies the
environment's mesh assets and worldbody geoms in, and absolutizes relative
mesh `file` paths (robot meshes resolve against the robot's `meshdir`;
environment meshes against the environment directory). Path policy: explicit
absolute paths are intentional and kept as-is; relative `..` traversals are
refused, as are `..` or absolute `<include>` paths.

Name clashes: environment mesh assets and geoms that collide with robot
names are namespaced with an `env_` prefix at compose time (robot geoms are
found at any body-tree depth), so MuJoCo's duplicate-name compile error
cannot occur from merging.

Verified 2026-09-19 with the pinned Go2 scene
(`.cache/menagerie/unitree_go2/scene.xml`, menagerie commit
`8161bba264d7fa7c99ca301e91e7fb44737676ad`), MuJoCo 3.13.0, Python 3.12.13
(`.cache/dimos/.venv`; `mujoco.__version__` printed by the actual test
run): the real-Blender-exported bundle (scene `scale_length=0.01`; rotated
collision cube, two translated visual crates, floor) composed with the Go2
scene loads in `mujoco.MjModel` from a different working directory
(absolute-path bypass of `meshdir`), steps 240 times with finite state, and
renders environment plus robot
(`output/blender_fixture/realbundle/go2_render.png`). Translation scaling is
verified by exported vertices landing at 0.9*0.01 = 0.009 world meters; the
rotated box half-extents match (cos35+sin35)/2 * 0.01 exactly. A clash
fixture with a robot owning `COL_crates` and `mesh_crate_visual` merges to
`env_`-prefixed names and compiles in MuJoCo. Unsafe mesh and include path
rejection is covered by the same fixture.

Limitations:

- Collision shapes are boxes only, axis-aligned in world space; rotated
  furniture gets a conservative approximation.
- UV coordinates and image textures are not exported; each object gets a
  single flat RGBA color, so source-scene texture fidelity is not preserved
  by design.
- `Material.use_nodes` triggers a deprecation warning on Blender 5.2
  (removal planned in 6.0); harmless today, revisit at the 6.0 upgrade.
