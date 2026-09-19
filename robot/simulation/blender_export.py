"""Blender scene -> MuJoCo environment asset bridge.

Two entry points:

1. Exporter (run inside Blender):

    blender --background scene.blend --python robot/simulation/blender_export.py -- \
        --output .data/simulation/blender/room

   Exports visual mesh objects (mesh type, in scene, not in the COLLISION
   collection and not named COL_*) to per-object OBJ files with triangulated
   geometry and world transforms applied, plus an MJCF environment.xml and a
   environmentbundle.json manifest. Objects in the COLLISION collection or
   with a COL_ name prefix contribute axis-aligned BOX collision geoms
   (world-space bounding box) instead of visual meshes.

2. Composer (run outside Blender with plain Python 3):

    python robot/simulation/blender_export.py compose \
        --environment .data/simulation/blender/room/environment.xml \
        --robot <go2-scene>.xml \
        --output <merged>.xml

   Merges the environment meshes/geoms into a copy of the robot MJCF so both
   load in one MuJoCo model. The robot file is read-only; merged mesh file
   references are resolved relative to the environment directory.

Units: Blender scenes must use the metric system; geometry is exported with
scene `unit_settings.scale_length` applied to every vertex and collision
bound, so a metric scene at any scale lands in meters. Z up matches MuJoCo's
default world orientation. Non-metric scenes are rejected. Materials become
flat RGBA colors; texture fidelity is not preserved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

BUNDLE_VERSION = 1
COLLISION_COLLECTION = "COLLISION"
COLLISION_PREFIX = "COL_"
VISUAL_GROUP = 2


def _warn(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _safe_name(obj) -> str:
    name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in obj.name)
    return name.strip("_") or "object"


def _rgba(obj) -> tuple[float, float, float, float]:
    """Flat viewport color from the first material, else a neutral gray."""
    for slot in obj.material_slots:
        mat = slot.material
        if mat and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    c = node.inputs["Base Color"]
                    if hasattr(c, "default_value"):
                        r, g, b = (float(v) for v in c.default_value[:3])
                        return (r, g, b, 1.0)
        elif mat:
            r, g, b = (float(v) for v in mat.diffuse_color[:3])
            return (r, g, b, 1.0)
    return (0.7, 0.7, 0.7, 1.0)


def _export() -> int:
    import bpy  # Only available inside Blender; kept out of module scope so
    # the compose CLI works with plain Python.

    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Export scene to MuJoCo bundle")
    parser.add_argument("--output", required=True, help="Output bundle directory")
    args = parser.parse_args(argv)

    out_root = os.path.abspath(args.output)
    os.makedirs(out_root, exist_ok=True)
    warnings: list[str] = []
    scene = bpy.context.scene

    unit_system = getattr(scene.unit_settings, "system", "METRIC")
    if unit_system != "METRIC":
        print(
            f"[blender_export] refusing non-metric scene units: {unit_system!r}",
            file=sys.stderr,
        )
        return 1
    unit_scale = float(getattr(scene.unit_settings, "scale_length", 1.0) or 1.0)

    collision_names: set[str] = set()
    for coll in bpy.data.collections:
        if coll.name == COLLISION_COLLECTION:
            collision_names.update(o.name for o in coll.objects)

    import mathutils

    mesh_dir = os.path.join(out_root, "meshes")
    os.makedirs(mesh_dir, exist_ok=True)

    def export_obj(obj, filepath: str) -> None:
        # Triangulated, world transforms applied: evaluate the object through
        # the depsgraph (modifiers applied) and bake matrix_world into the
        # mesh vertices before handing the single object to the operator.
        # Axis mapping Y/Z is the identity for Blender world space (verified
        # empirically in Blender 5.2: NEGATIVE_Y rotates 180 deg about Z),
        # keeping Z-up meters in the OBJ exactly as MuJoCo expects.
        # world_scale folds the scene unit scale into every world-space
        # coordinate including translation: scale must be the LEFT factor,
        # otherwise object origins keep their unscaled world position.
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        world_scale = _scale_matrix(unit_scale) @ evaluated.matrix_world
        mesh.transform(world_scale)
        # An evaluated (temporary) mesh datablock cannot be linked into the
        # main database; the operator needs an owned copy.
        owned = mesh.copy()
        temp = bpy.data.objects.new("export_tmp_" + evaluated.name, owned)
        try:
            bpy.context.scene.collection.objects.link(temp)
            for existing in bpy.context.selected_objects:
                existing.select_set(False)
            temp.select_set(True)
            bpy.context.view_layer.objects.active = temp
            bpy.ops.wm.obj_export(
                filepath=filepath,
                export_selected_objects=True,
                apply_modifiers=True,
                export_triangulated_mesh=True,
                export_uv=False,
                export_normals=True,
                export_materials=False,
                forward_axis="Y",
                up_axis="Z",
                export_animation=False,
            )
        finally:
            bpy.data.objects.remove(temp, do_unlink=True)
            bpy.data.meshes.remove(owned)
            evaluated.to_mesh_clear()

    def _scale_matrix(s):
        from mathutils import Matrix

        return Matrix.Diagonal((s, s, s, 1.0))

    env = ET.Element("mujoco", model="blender_environment")
    ET.SubElement(env, "compiler", angle="radian", meshdir=".", autolimits="true")
    asset = ET.SubElement(env, "asset")
    worldbody = ET.SubElement(env, "worldbody")
    objects: list[dict] = []
    seen_names: set[str] = set()

    for obj in scene.objects:
        if obj.type != "MESH" or not obj.data or not len(obj.data.polygons):
            continue
        is_collision = obj.name in collision_names or obj.name.startswith(
            COLLISION_PREFIX
        )
        base = _safe_name(obj)
        unique = base
        n = 2
        while unique in seen_names:
            unique = f"{base}_{n}"
            n += 1
        seen_names.add(unique)

        if is_collision:
            # Min/max over all 8 corners: bound_box corner order is not
            # sorted, and rotated objects need true extrema, not pairs.
            bb = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
            mins = (min(v.x for v in bb), min(v.y for v in bb), min(v.z for v in bb))
            maxs = (max(v.x for v in bb), max(v.y for v in bb), max(v.z for v in bb))
            center = tuple((lo + hi) / 2.0 for lo, hi in zip(mins, maxs))
            # MJCF box size is the HALF extent: divide the full extent once.
            size = tuple((hi - lo) / 2.0 for lo, hi in zip(mins, maxs))
            geom = ET.SubElement(
                worldbody,
                "geom",
                name=unique,
                type="box",
                pos=" ".join(f"{v * unit_scale:.6f}" for v in center),
                size=" ".join(f"{v * unit_scale:.6f}" for v in size),
                rgba="1 0 0 0.15",
                contype="1",
                conaffinity="1",
            )
            if geom is not None:
                _warn(
                    warnings,
                    f"collision geom '{unique}' is an axis-aligned bounding-box "
                    "approximation of the mesh",
                )
            objects.append(
                {
                    "name": unique,
                    "role": "collision",
                    "type": "box",
                }
            )
        else:
            mesh_name = "mesh_" + unique
            obj_path = os.path.join(mesh_dir, mesh_name + ".obj")
            export_obj(obj, obj_path)
            ET.SubElement(
                asset,
                "mesh",
                name=mesh_name,
                file="meshes/" + mesh_name + ".obj",
            )
            ET.SubElement(
                worldbody,
                "geom",
                name=unique,
                type="mesh",
                mesh=mesh_name,
                pos="0 0 0",
                rgba="{:.4f} {:.4f} {:.4f} {:.4f}".format(*_rgba(obj)),
                contype="0",
                conaffinity="0",
                group=str(VISUAL_GROUP),
            )
            objects.append(
                {
                    "name": unique,
                    "role": "visual",
                    "type": "mesh",
                    "file": "meshes/" + mesh_name + ".obj",
                }
            )

    if not objects:
        _warn(warnings, "no exportable mesh objects found in the scene")

    ET.indent(env, space="  ")
    tree = ET.ElementTree(env)
    tree.write(
        os.path.join(out_root, "environment.xml"),
        encoding="unicode",
        xml_declaration=False,
    )

    bundle = {
        "version": BUNDLE_VERSION,
        "units": "m",
        "up_axis": "Z",
        "mjcf": "environment.xml",
        "source": "blender",
        "objects": objects,
        "warnings": warnings,
    }
    with open(
        os.path.join(out_root, "environmentbundle.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(bundle, fh, indent=2)
        fh.write("\n")
    print(f"[blender_export] wrote bundle to {out_root} ({len(objects)} objects)")
    return 0


def _compose(environment_xml: str, robot_xml: str, output_xml: str) -> int:
    env_dir = os.path.dirname(os.path.abspath(environment_xml))
    robot_dir = os.path.dirname(os.path.abspath(robot_xml))
    env_tree = ET.parse(environment_xml)
    robot_tree = ET.parse(robot_xml)
    env_root = env_tree.getroot()
    robot_root = robot_tree.getroot()

    def inline_includes(root: ET.Element, base_dir: str) -> None:
        # MuJoCo resolves <include> relative to the top-level file's
        # directory; the merged output may live elsewhere, so splice the
        # included files in ourselves, recursively.
        for inc in list(root.findall("include")):
            inc_file = inc.get("file", "")
            if os.path.isabs(inc_file) or ".." in inc_file.replace(os.sep, "/").split(
                "/"
            ):
                print(
                    f"compose: refusing unsafe include path {inc_file!r}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            inc_path = os.path.normpath(os.path.join(base_dir, inc_file))
            sub_root = ET.parse(inc_path).getroot()
            inline_includes(sub_root, os.path.dirname(inc_path))
            index = list(root).index(inc)
            for child in list(sub_root):
                root.insert(index, child)
                index += 1
            root.remove(inc)

    inline_includes(robot_root, robot_dir)

    # Absolutize every mesh path so the merged model loads from any working
    # directory and the robot's own compiler meshdir stays untouched. Relative
    # environment meshes resolve against the environment directory; robot
    # meshes resolve against the robot's compiler meshdir.
    robot_compiler = robot_root.find("compiler")
    robot_meshdir = (
        robot_compiler.get("meshdir") if robot_compiler is not None else None
    ) or "."

    def absolutize(elem: ET.Element, base_dir: str) -> None:
        """Resolve relative mesh paths to absolute, refusing `..` escapes.

        Absolute paths are intentional and pass through unchanged: the
        author explicitly wrote them, and MuJoCo resolves absolute paths
        without `meshdir`. Only relative traversals are refused.
        """
        file_attr = elem.get("file")
        if file_attr is None:
            return
        if not os.path.isabs(file_attr):
            if ".." in file_attr.replace(os.sep, "/").split("/"):
                print(
                    f"compose: refusing unsafe mesh path {file_attr!r}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            elem.set("file", os.path.normpath(os.path.join(base_dir, file_attr)))

    for elem in env_root.findall("asset/mesh"):
        absolutize(elem, env_dir)
    for elem in robot_root.findall("asset/mesh"):
        absolutize(elem, os.path.normpath(os.path.join(robot_dir, robot_meshdir)))
    for elem in robot_root.findall("asset/mesh"):
        if elem.get("file") and not os.path.isfile(elem.get("file")):
            print(
                f"compose: warning: robot mesh not found: {elem.get('file')}",
                file=sys.stderr,
            )

    robot_assets = robot_root.find("asset")
    if robot_assets is None:
        robot_assets = ET.SubElement(robot_root, "asset")
    for elem in env_root.findall("asset/mesh"):
        # MuJoCo fails at compile time on duplicate asset names; namespace
        # environment mesh assets and update the geoms that reference them.
        old_name = elem.get("name")
        new_name = "env_" + old_name
        if new_name != old_name:
            elem.set("name", new_name)
            for geom in env_root.findall("worldbody/geom"):
                if geom.get("mesh") == old_name:
                    geom.set("mesh", new_name)
        robot_assets.append(elem)

    robot_asset_names = {
        a.get("name") for a in robot_assets.findall("mesh") if a.get("name")
    }
    robot_geom_names = {g.get("name") for g in robot_root.iter("geom") if g.get("name")}
    robot_body = robot_root.find("worldbody")
    if robot_body is None:
        robot_body = ET.SubElement(robot_root, "worldbody")
    for elem in list(env_root.findall("worldbody/geom")):
        geom_name = elem.get("name")
        if geom_name and geom_name in robot_geom_names:
            new_name = "env_" + geom_name
            n = 2
            while new_name in robot_geom_names:
                new_name = f"env_{geom_name}_{n}"
                n += 1
            elem.set("name", new_name)
            robot_geom_names.add(new_name)
        elif geom_name:
            robot_geom_names.add(geom_name)
        mesh_ref = elem.get("mesh")
        if mesh_ref:
            robot_asset_names.add(mesh_ref)
        robot_body.append(elem)

    os.makedirs(os.path.dirname(os.path.abspath(output_xml)) or ".", exist_ok=True)
    ET.indent(robot_tree, space="  ")
    robot_tree.write(output_xml, encoding="unicode", xml_declaration=False)
    print(f"compose: wrote {output_xml}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "compose":
        parser = argparse.ArgumentParser(prog="blender_export compose")
        parser.add_argument("--environment", required=True)
        parser.add_argument("--robot", required=True)
        parser.add_argument("--output", required=True)
        args = parser.parse_args(sys.argv[2:])
        return _compose(args.environment, args.robot, args.output)
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(__doc__)
        return 0
    return _export()


if __name__ == "__main__":
    sys.exit(main())
