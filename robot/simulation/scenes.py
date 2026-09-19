"""Deterministic, local-only furnished MJCF scenes using cached Unitree Go2 assets.

Ground truth is authored simulator metadata, never a perception/VLM result.
No additional joints are introduced; the original robot home keyframe is retained.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CATEGORIES = ("safe_bed", "floor_lying", "seated", "standing", "occluded", "empty")
GENERATOR_VERSION = 2


def numbers(values):
    return " ".join(f"{v:.6f}" for v in values)


def element(parent, tag, **attrs):
    return ET.SubElement(
        parent,
        tag,
        {
            k: numbers(v) if isinstance(v, (tuple, list)) else str(v)
            for k, v in attrs.items()
        },
    )


def geom(parent, name, kind, pos, size, color, **attrs):
    return element(
        parent,
        "geom",
        name="env_" + name,
        type=kind,
        pos=pos,
        size=size,
        rgba=color,
        **attrs,
    )


def box(parent, name, pos, size, color):
    return geom(parent, name, "box", pos, size, color)


def mannequin(world, location, posture, shirt):
    body = element(world, "body", name="env_resident", pos=location)
    if posture == "lying":
        # Local z is the person's long axis, rotated along world y.
        body.set("quat", numbers((math.sqrt(0.5), -math.sqrt(0.5), 0, 0)))
    skin = (0.69, 0.46, 0.32, 1)
    pants = (0.17, 0.23, 0.32, 1)
    seated = posture == "seated"
    hip = 0.59 if seated else 0.86
    geom(
        body,
        "resident_torso",
        "ellipsoid",
        (0, 0, hip + 0.27),
        (0.22, 0.13, 0.3),
        shirt,
    )
    geom(
        body,
        "resident_head",
        "ellipsoid",
        (0, 0, hip + 0.73),
        (0.14, 0.125, 0.175),
        skin,
    )
    geom(
        body,
        "resident_hair",
        "ellipsoid",
        (0, 0.022, hip + 0.81),
        (0.143, 0.115, 0.105),
        (0.73, 0.72, 0.69, 1),
    )
    for side in (-1, 1):
        name = "left" if side < 0 else "right"

        def limb(part, a, b, radius, color, name=name):
            element(
                body,
                "geom",
                name=f"env_resident_{name}_{part}",
                type="capsule",
                fromto=(*a, *b),
                size=(radius,),
                rgba=color,
            )

        limb(
            "upper_arm",
            (side * 0.24, 0, hip + 0.46),
            (side * 0.29, -0.035, hip + 0.18),
            0.063,
            shirt,
        )
        limb(
            "forearm",
            (side * 0.29, -0.035, hip + 0.18),
            (side * 0.26, -0.18 if seated else -0.035, hip - 0.08),
            0.052,
            skin,
        )
        knee = (side * 0.115, -0.39, 0.53) if seated else (side * 0.115, 0, 0.47)
        ankle = (side * 0.115, -0.42, 0.10) if seated else (side * 0.115, 0, 0.10)
        limb("thigh", (side * 0.115, 0, hip), knee, 0.085, pants)
        limb("shin", knee, ankle, 0.065, pants)
        geom(
            body,
            f"resident_{name}_shoe",
            "ellipsoid",
            (ankle[0], ankle[1] - 0.065, 0.065),
            (0.08, 0.14, 0.065),
            (0.15, 0.12, 0.1, 1),
        )


def cached_visuals():
    root = Path(__file__).resolve().parents[2]
    path = root / ".cache/simulation-assets/manifest.json"
    if not path.exists():
        return {}
    entries = json.loads(path.read_text()).get("assets", {})
    return {
        key: value
        for key, value in entries.items()
        if all(
            (root / part["mesh"]).resolve().is_relative_to(root)
            and (root / part["mesh"]).is_file()
            and (
                not part.get("texture")
                or (
                    (root / part["texture"]).resolve().is_relative_to(root)
                    and (root / part["texture"]).is_file()
                )
            )
            for part in value.get("parts", [])
        )
        and (
            not value.get("texture")
            or (
                (root / value["texture"]).resolve().is_relative_to(root)
                and (root / value["texture"]).is_file()
            )
        )
    }


def mesh_visual(asset, world, cached, key, position, quaternion=None):
    root = Path(__file__).resolve().parents[2]
    body = element(world, "body", name="env_" + key + "_visual", pos=position)
    if quaternion:
        body.set("quat", numbers(quaternion))
    for i, part in enumerate(cached[key]["parts"]):
        name = f"env_{key}_scan_{i}"
        element(asset, "mesh", name=name, file=str(root / part["mesh"]))
        kwargs = {}
        if part.get("texture"):
            element(
                asset, "texture", name=name, type="2d", file=str(root / part["texture"])
            )
            element(
                asset,
                "material",
                name=name,
                texture=name,
                specular=".08",
                shininess=".1",
            )
            kwargs["material"] = name
        element(
            body,
            "geom",
            name=name,
            type="mesh",
            mesh=name,
            contype=0,
            conaffinity=0,
            group=2,
            **kwargs,
        )
    return body


def observation_schedule(category):
    """Planned observer windows, not actions, human animation, or clinical timing."""
    phases = [(0, "Settle"), (3, "Observation window")]
    if category == "floor_lying":
        phases += [
            (5, "Planned check-in"),
            (35, "Planned reply window"),
            (65, "Planned family review"),
        ]
        duration = 120
    else:
        phases += [(60, "Planned routine check"), (120, "Extended observation")]
        duration = 300
    return {
        "timingkind": "scenario_schedule_not_executed_actions",
        "scenario_duration_s": duration,
        "timeline": [{"start_s": second, "label": label} for second, label in phases],
    }


def make_scene(assets: Path, category: str, variant: int, seed: int):
    rng = random.Random(seed)
    cached = cached_visuals()
    root = ET.parse(assets / "go2.xml").getroot()
    root.set("model", f"annie_{category}_{variant:03d}")
    root.find("compiler").set("meshdir", str((assets / "assets").resolve()))
    width, depth = rng.uniform(5.6, 6.5), rng.uniform(4.7, 5.5)
    element(root, "statistic", center=(0, 0, 0.6), extent=max(width, depth))
    visual = element(root, "visual")
    element(
        visual,
        "headlight",
        ambient=(0.30, 0.30, 0.30),
        diffuse=(0.35, 0.35, 0.35),
        specular=(0.15, 0.15, 0.15),
    )
    element(visual, "rgba", haze=(0.88, 0.86, 0.82, 1))
    element(visual, "global", offwidth=960, offheight=720)
    asset = root.find("asset")
    element(
        asset,
        "texture",
        name="env_sky",
        type="skybox",
        builtin="gradient",
        rgb1=(0.83, 0.85, 0.86),
        rgb2=(0.94, 0.91, 0.85),
        width=256,
        height=1024,
    )
    world = root.find("worldbody")
    element(
        world,
        "light",
        name="env_daylight",
        pos=(-1, -1, 4),
        dir=(0.3, 0.4, -1),
        directional="true",
        diffuse=(rng.uniform(0.45, 0.65), 0.52, 0.48),
    )
    wood = rng.choice(
        [(0.53, 0.36, 0.23, 1), (0.63, 0.47, 0.3, 1), (0.4, 0.29, 0.21, 1)]
    )
    fabric = rng.choice(
        [(0.42, 0.46, 0.43, 1), (0.45, 0.43, 0.40, 1), (0.48, 0.40, 0.34, 1)]
    )
    wall = (0.88, 0.86, 0.80, 1)
    geom(
        world,
        "floor",
        "plane",
        (0, 0, 0),
        (width / 2, depth / 2, 0.1),
        (0.66, 0.61, 0.53, 1),
    )
    box(world, "back_wall", (0, depth / 2, 0.7), (width / 2, 0.055, 0.7), wall)
    box(world, "left_wall", (-width / 2, 0, 0.7), (0.055, depth / 2, 0.7), wall)
    box(world, "right_wall", (width / 2, 0, 0.7), (0.055, depth / 2, 0.7), wall)
    if "floor" in cached:
        element(
            asset,
            "texture",
            name="env_wood_floor",
            type="2d",
            file=str(Path(__file__).resolve().parents[2] / cached["floor"]["texture"]),
        )
        element(
            asset,
            "material",
            name="env_wood_floor",
            texture="env_wood_floor",
            texrepeat=(3, 3),
            texuniform="true",
            reflectance=".04",
        )
        floor = world.find('geom[@name="env_floor"]')
        floor.set("material", "env_wood_floor")
        floor.set("rgba", "1 1 1 1")
    for suffix, position, size in [
        ("back", (0, depth / 2 - 0.065, 0.07), (width / 2, 0.025, 0.07)),
        ("left", (-width / 2 + 0.065, 0, 0.07), (0.025, depth / 2, 0.07)),
        ("right", (width / 2 - 0.065, 0, 0.07), (0.025, depth / 2, 0.07)),
    ]:
        box(world, "skirting_" + suffix, position, size, (0.93, 0.91, 0.87, 1))
    box(
        world,
        "window_frame",
        (0.25, depth / 2 - 0.08, 1.01),
        (0.72, 0.045, 0.32),
        (0.94, 0.94, 0.91, 1),
    )
    box(
        world,
        "window_glass",
        (0.25, depth / 2 - 0.13, 1.01),
        (0.65, 0.01, 0.26),
        (0.57, 0.69, 0.74, 1),
    )
    box(
        world,
        "window_mullion",
        (0.25, depth / 2 - 0.15, 1.01),
        (0.022, 0.015, 0.27),
        (0.94, 0.94, 0.91, 1),
    )
    box(
        world, "rug", (-0.2, -0.15, 0.004), (0.94, 0.72, 0.004), (0.43, 0.40, 0.35, 1)
    ).set("contype", "0")
    bx, by = -width / 2 + 0.86, depth / 2 - 1.3
    box(world, "bed_frame", (bx, by, 0.24), (0.7, 1.08, 0.18), wood)
    box(
        world, "bed_mattress", (bx, by, 0.49), (0.69, 1.05, 0.095), (0.9, 0.89, 0.83, 1)
    )
    box(world, "bed_blanket", (bx, by - 0.26, 0.59), (0.7, 0.72, 0.025), fabric)
    box(
        world,
        "bed_pillow",
        (bx, by + 0.75, 0.62),
        (0.48, 0.21, 0.06),
        (0.98, 0.96, 0.89, 1),
    )
    box(world, "bed_headboard", (bx, by + 1.12, 0.65), (0.73, 0.07, 0.55), wood)
    cx, cy = width / 2 - 0.9, depth / 2 - 0.9
    box(world, "chair_seat", (cx, cy, 0.48), (0.36, 0.35, 0.07), fabric)
    box(world, "chair_back", (cx, cy + 0.32, 0.87), (0.36, 0.06, 0.35), fabric)
    for j, (dx, dy) in enumerate(
        [(-0.27, -0.26), (0.27, -0.26), (-0.27, 0.26), (0.27, 0.26)]
    ):
        box(
            world,
            f"chair_leg_{j}",
            (cx + dx, cy + dy, 0.23),
            (0.045, 0.045, 0.23),
            wood,
        )
    tx, ty = rng.uniform(0.05, 0.3), depth / 2 - 0.65
    box(world, "table_top", (tx, ty, 0.75), (0.55, 0.4, 0.045), wood)
    for j, (dx, dy) in enumerate(
        [(-0.45, -0.3), (0.45, -0.3), (-0.45, 0.3), (0.45, 0.3)]
    ):
        box(world, f"table_leg_{j}", (tx + dx, ty + dy, 0.35), (0.04, 0.04, 0.35), wood)
    mug = (tx + 0.22, ty - 0.08, 0.845)
    geom(world, "mug", "cylinder", mug, (0.055, 0.05), (0.85, 0.36, 0.19, 1))
    # Two flattened lens rims and a bridge make glasses recognizable from above.
    glasses = (tx - 0.23, ty - 0.12, 0.808)
    for j, dx in enumerate((-0.058, 0.058)):
        geom(
            world,
            f"glasses_lens_{j}",
            "ellipsoid",
            (glasses[0] + dx, glasses[1], glasses[2]),
            (0.05, 0.036, 0.009),
            (0.13, 0.18, 0.22, 1),
        )
    box(world, "glasses_bridge", glasses, (0.018, 0.008, 0.006), (0.1, 0.1, 0.1, 1))
    lx, ly = cx - 0.65, cy + 0.22
    geom(
        world,
        "lamp_base",
        "cylinder",
        (lx, ly, 0.035),
        (0.18, 0.035),
        (0.25, 0.25, 0.23, 1),
    )
    geom(
        world,
        "lamp_stem",
        "cylinder",
        (lx, ly, 0.68),
        (0.022, 0.64),
        (0.37, 0.34, 0.26, 1),
    )
    geom(
        world,
        "lamp_shade",
        "ellipsoid",
        (lx, ly, 1.35),
        (0.24, 0.24, 0.2),
        (0.96, 0.79, 0.46, 1),
    )
    rx, ry = rng.uniform(-0.25, 0.25), -depth / 2 + 1
    qpos = [float(x) for x in root.find("keyframe/key").get("qpos").split()]
    qpos[:2] = [rx, ry]
    root.find("keyframe/key").set("qpos", numbers(qpos))
    root.find("keyframe/key").set("ctrl", numbers([0] * 12))
    root.find('worldbody/body[@name="base"]').set("pos", numbers((rx, ry, 0.27)))
    location, posture = None, None
    if category == "safe_bed":
        location, posture = (bx, by - 0.83, 0.74), "lying"
    elif category == "floor_lying":
        location, posture = (0.1, -0.8 + rng.uniform(-0.15, 0.15), 0.16), "lying"
    elif category == "seated":
        location, posture = (cx, cy - 0.05, 0), "seated"
    elif category == "standing":
        location, posture = (
            (rng.uniform(-0.25, 0.25), rng.uniform(-0.25, 0.2), 0),
            "standing",
        )
    elif category == "occluded":
        location, posture = (cx, cy - 0.05, 0), "seated"
        box(
            world,
            "occluder",
            (cx, cy - 0.73, 0.65),
            (0.56, 0.055, 0.65),
            (0.48, 0.57, 0.52, 1),
        )
    for key, center in [("chair", (cx, cy, 0)), ("table", (tx, ty, 0))]:
        if key in cached:
            for old in world.findall("geom"):
                if old.get("name", "").startswith("env_" + key + "_"):
                    old.set("group", "3")
            mesh_visual(asset, world, cached, key, center)
    for j, (dx, dy) in enumerate(
        [(-0.6, -0.92), (0.6, -0.92), (-0.6, 0.92), (0.6, 0.92)]
    ):
        box(
            world,
            f"bed_leg_{j}",
            (bx + dx, by + dy, 0.075),
            (0.065, 0.065, 0.075),
            wood,
        )
    if location:
        if "person" in cached and posture != "seated":
            if posture == "lying":
                # Rotate upright scan onto its back; support based on exact mesh depth.
                bottom = -cached["person"]["bounds"][1][0]
                support = 0.68 if category == "safe_bed" else 0.012
                location = (location[0], location[1], support - bottom)
                rotation = (0.5, -0.5, 0.5, 0.5)
            else:
                rotation = None
            mesh_visual(asset, world, cached, "person", location, rotation)
        else:
            mannequin(world, location, posture, fabric)
    titles = {
        "safe_bed": "Resting in bed",
        "floor_lying": "Lying on the floor",
        "seated": "Seated in a chair",
        "standing": "Standing at home",
        "occluded": "Partly hidden resident",
        "empty": "Unoccupied room",
    }
    ground_truth = {
        "source": "authored_simulator_labels_not_vlm_output",
        "resident_present": location is not None,
        "posture": posture,
        "resident_origin_m": location,
        "support_surface": "bed"
        if category == "safe_bed"
        else "chair"
        if category in ("seated", "occluded")
        else "floor"
        if location
        else None,
        "occluder_present": category == "occluded",
        "visibility": "not_measured_for_camera",
        "objects": {
            "bed": (bx, by, 0.49),
            "chair": (cx, cy, 0.48),
            "table": (tx, ty, 0.75),
            "lamp": (lx, ly, 0.68),
            "glasses": glasses,
            "mug": mug,
        },
        "robot_home_position_m": qpos[:3],
        "room_size_m": (width, depth, 1.4),
    }
    ident = f"{category}-{variant:03d}"
    record = {
        "id": ident,
        "title": f"{titles[category]} · {variant + 1:02d}",
        "description": "Synthetic furnished home with cached textured assets when available. Fixed resident poses; no VLM inference or medical assessment.",
        "visual_quality": {
            "mode": "textured_assets" if cached else "procedural_fallback",
            "assets": {
                k: {"source": v["source"], "license": v["license"]}
                for k, v in cached.items()
            },
            "resident": "textured_rigid_scan"
            if "person" in cached and posture not in (None, "seated")
            else "procedural_mannequin"
            if posture
            else "absent",
        },
        "file": ident + ".xml",
        "category": category,
        "seed": seed,
        "ground_truth": ground_truth,
        "overview": {
            "lookat": [0, 0, 0.5],
            "distance": max(width, depth) * 1.25,
            "azimuth": 105,
            "elevation": -48,
        },
    }
    record.update(observation_schedule(category))
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n", record


def atomic_write(path: Path, content: str):
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix="." + path.name,
        delete=False,
    ) as stream:
        temp = Path(stream.name)
        stream.write(content)
    temp.replace(path)


def generate_batch(assets: Path, output: Path, count: int = 24, seed: int = 2026):
    if type(count) is not int or not 1 <= count <= 200:
        raise ValueError("count must be an integer between 1 and 200")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("seed must be an integer between 0 and 2147483647")
    assets, output = Path(assets).resolve(), Path(output).resolve()
    if not (assets / "go2.xml").is_file():
        raise ValueError(f"Missing cached Go2 source: {assets / 'go2.xml'}")
    output.mkdir(parents=True, exist_ok=True)
    scenes = []
    for index in range(count):
        category = CATEGORIES[index % len(CATEGORIES)]
        content, record = make_scene(
            assets, category, index // len(CATEGORIES), seed + index * 1009
        )
        target = output / record["file"]
        if (
            target.exists()
            and 'model="annie_' not in target.read_text(encoding="utf-8")[:200]
        ):
            raise ValueError(f"Refusing to replace non-generated scene: {target}")
        atomic_write(target, content)
        scenes.append(record)
    manifest = {
        "version": 1,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "count": count,
        "asset_root": str(assets),
        "source_model": str(assets / "go2.xml"),
        "scenes": scenes,
    }
    atomic_write(output / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    try:
        manifest = generate_batch(args.assets, args.output, args.count, args.seed)
    except (ValueError, OSError, ET.ParseError) as exc:
        parser.error(str(exc))
    print(f"Generated {manifest['count']} scenes: {args.output / 'manifest.json'}")


if __name__ == "__main__":
    main()
