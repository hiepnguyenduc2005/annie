"""Furnished apartment demo scene for the Go2: CC0 textured assets, warm lighting, capsule residents.

Two steps, both explicit:

    PY=.cache/dimos/.venv/bin/python
    $PY robot/simulation/apartment.py --fetch   # network: Poly Haven CC0 models/textures -> ignored cache
    $PY robot/simulation/apartment.py --build   # no network: writes assets/apartment/apartment_*.xml

The XML files reference the cached Go2 model and the cached assets by repo-relative paths, so
they load with ``MjModel.from_xml_path`` on any checkout that has run the two steps above.
``load_model`` additionally attaches the ``robot_front`` camera to the Go2 ``base`` body (the
static XML includes go2.xml untouched, so the camera is added through ``MjSpec``).

People are capsule mannequins in fixed poses: authored simulator props, not animated humans.
"Jeanine" wears a saturated red top because the demo's target identifier
(``robot/dog/perception/target_id.py``) is a shirt-colour match, not an identification.
Sources and licenses: ``robot/simulation/assets/apartment/SOURCES.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache/simulation-assets/apartment"
TEXTURE_DIR = ROOT / ".cache/simulation-assets"
GO2_DIR = ROOT / ".cache/menagerie/unitree_go2"
OUT_DIR = Path(__file__).resolve().parent / "assets/apartment"
MANIFEST = CACHE / "manifest.json"
LIMIT = 200 * 1024 * 1024
VARIANTS = ("seated", "floor", "empty")

# key -> (Poly Haven id, target height in metres or None to keep the authored scale)
MODELS = {
    "sofa": ("sofa_03", None),
    "dining_chair": ("dining_chair_02", None),
    "dining_table": ("small_wooden_table_01", 0.76),
    "coffee_table": ("modern_coffee_table_01", None),
    "side_table": ("side_table_01", None),
    "plant": ("potted_plant_04", None),
    "pendant": ("modern_ceiling_lamp_01", None),
    "frame_a": ("hanging_picture_frame_01", None),
    "frame_b": ("hanging_picture_frame_02", None),
    "clock": ("wall_clock", None),
    "vase": ("ceramic_vase_01", None),
    "cabinet": ("painted_wooden_cabinet", None),
}
# key -> (Poly Haven id, metres covered by one texture repeat)
TEXTURES = {
    "plaster": ("white_plaster_02", 2.0),
    "parquet": ("herringbone_parquet", 1.6),
    "tiles": ("floor_tiles_06", 1.2),
    "backsplash": ("long_white_tiles", 0.9),
    "marble": ("marble_01", 1.5),
    "oak": ("oak_veneer_01", 1.2),
    "rug": ("wool_boucle", 0.7),
}

# Scripted forward-camera paths per variant: (x, y, yaw_deg) waypoints on the floor plane.
PATHS = {
    "seated": [(-4.5, -1.45, 0), (-3.0, -1.45, 0), (-2.2, -1.3, 40), (-1.75, -0.55, 62), (-1.5, 0.25, 78)],
    "floor": [(-4.5, -1.45, 0), (-3.0, -1.45, 0), (-2.4, -1.35, 35), (-2.15, -1.0, 52), (-2.0, -0.8, 58)],
    "empty": [(-4.5, -1.45, 0), (-3.0, -1.45, 0), (-2.2, -1.3, 40), (-1.75, -0.55, 62), (-1.5, 0.25, 78)],
}
CAMERA = {"name": "robot_front", "pos": (0.34, 0.0, 0.07), "xyaxes": (0, -1, 0, 0, 0, 1), "fovy": 85}


# --------------------------------------------------------------------------- assets (network)

def prepare_assets():
    """Download the CC0 Poly Haven files into the ignored cache and convert glTF -> textured OBJ parts."""
    import numpy as np
    import trimesh
    from PIL import Image

    CACHE.mkdir(parents=True, exist_ok=True)
    records, used = [], 0

    def fetch(url, path):
        nonlocal used
        if not path.resolve().is_relative_to(CACHE.resolve()):
            raise ValueError("asset path escapes cache")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"api.polyhaven.com", "dl.polyhaven.org"}:
            raise ValueError(f"unsupported asset source URL: {url}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            request = urllib.request.Request(url, headers={"User-Agent": "Annie-local-simulator/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(LIMIT - used + 1)
            used += len(data)
            if used > LIMIT:
                raise ValueError("asset download budget exceeded")
            path.write_bytes(data)
        records.append({"url": url, "path": str(path.relative_to(ROOT)),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
        return path

    models = {}
    for key, (ident, height) in MODELS.items():
        base = CACHE / ident
        api = fetch("https://api.polyhaven.com/files/" + ident, base / "files.json")
        info = json.loads(api.read_text())["gltf"]["1k"]["gltf"]
        gltf = fetch(info["url"], base / (ident + ".gltf"))
        for name, item in info["include"].items():
            fetch(item["url"], base / name)
        scene = trimesh.load(gltf, force="scene")
        parts = []
        for node in scene.graph.nodes_geometry:
            transform, name = scene.graph[node]
            mesh = scene.geometry[name].copy()
            mesh.apply_transform(transform)
            mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # glTF Y-up -> Z-up
            parts.append(mesh)
        bounds = np.array([p.bounds for p in parts])
        low, high = bounds[:, 0].min(axis=0), bounds[:, 1].max(axis=0)
        scale = (height / (high[2] - low[2])) if height else 1.0
        center = (low + high) / 2
        center[2] = low[2]  # origin at the footprint centre, on the floor
        entries = []
        for i, mesh in enumerate(parts):
            mesh.vertices = (mesh.vertices - center) * scale
            obj, texture = base / f"mjcf_{i}.obj", base / f"mjcf_{i}.png"
            image = getattr(mesh.visual.material, "baseColorTexture", None)
            if image is not None:
                image.convert("RGB").save(texture)
            factor = getattr(mesh.visual.material, "baseColorFactor", None)
            obj.write_text(trimesh.exchange.obj.export_obj(mesh, include_texture=True))
            entries.append({"mesh": str(obj.relative_to(ROOT)),
                            "texture": str(texture.relative_to(ROOT)) if image is not None else None,
                            "rgba": [round(float(v) / 255.0, 3) for v in factor] if factor is not None else None,
                            "faces": int(len(mesh.faces))})
        models[key] = {"source": "https://polyhaven.com/a/" + ident, "license": "CC0", "parts": entries,
                       "size_m": [round(float(v), 3) for v in (high - low) * scale]}

    textures = {}
    for key, (ident, tile_m) in TEXTURES.items():
        api = fetch("https://api.polyhaven.com/files/" + ident, CACHE / "textures" / (ident + ".json"))
        files = json.loads(api.read_text())
        # Most Poly Haven textures publish "Diffuse"; a few fabrics publish colour variants (col_1, col_2, ...).
        diffuse = files.get("Diffuse") or next(
            (files[k] for k in sorted(files) if k.lower().startswith(("col", "diff"))), None)
        if diffuse is None:
            raise ValueError(f"no colour map published for {ident}: {sorted(files)}")
        jpg = fetch(diffuse["1k"]["jpg"]["url"], CACHE / "textures" / (ident + ".jpg"))
        png = CACHE / "textures" / (ident + ".png")
        Image.open(jpg).convert("RGB").save(png)  # MuJoCo reads PNG, not JPEG
        textures[key] = {"source": "https://polyhaven.com/a/" + ident, "license": "CC0",
                         "texture": str(png.relative_to(ROOT)), "tile_m": tile_m}

    textures.update(_generated_textures())
    for path in sorted(CACHE.rglob("*")):
        if path.is_file() and path.name != "manifest.json" and not any(r["path"] == str(path.relative_to(ROOT)) for r in records):
            records.append({"url": None, "derived_from": "source files in this manifest or generated by apartment.py",
                            "path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "bytes": path.stat().st_size})
    MANIFEST.write_text(json.dumps({"version": 1, "credit": "Powered by Poly Haven (CC0)", "models": models,
                                    "textures": textures, "files": records}, indent=2) + "\n")
    print(json.dumps({"downloaded_bytes": used, "models": list(models), "textures": list(textures), "manifest": str(MANIFEST)}))


def _generated_textures():
    """Small procedural textures written by this script (Annie's own work, no third-party source)."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    out = CACHE / "generated"
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(2026)

    def knit(path, rgb):
        n = 256
        yy, xx = np.mgrid[0:n, 0:n]
        weave = 0.06 * np.sin(xx * 2 * np.pi / 8) * np.cos(yy * 2 * np.pi / 6) + rng.normal(0, 0.025, (n, n))
        img = np.clip(np.array(rgb)[None, None, :] * (1.0 + weave[..., None]), 0, 255).astype(np.uint8)
        Image.fromarray(img).save(path)

    knit(out / "shirt_red.png", (214, 24, 30))      # hue ~358 deg, S ~0.89: inside the identifier's red band
    knit(out / "shirt_teal.png", (52, 116, 128))
    knit(out / "trousers_navy.png", (44, 52, 78))
    knit(out / "trousers_grey.png", (92, 90, 94))
    knit(out / "linen.png", (226, 214, 192))        # curtains: deliberately not red (the identifier keys on red)

    w, h = 512, 384  # bright overexposed daylight with soft foliage: what a window looks like from indoors
    sky = np.linspace(0, 1, h)[:, None, None]
    img = (np.array([168, 203, 238]) * (1 - sky) + np.array([250, 250, 244]) * sky) * np.ones((h, w, 1))
    view = Image.fromarray(img.astype(np.uint8))
    draw = ImageDraw.Draw(view)
    for _ in range(26):
        cx, cy, r = rng.integers(0, w), rng.integers(int(h * 0.62), h), rng.integers(30, 80)
        g = int(rng.integers(105, 160))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(g - 50, g, g - 62))
    view.filter(ImageFilter.GaussianBlur(7)).save(out / "window_view.png")

    art = Image.new("RGB", (256, 320), (233, 226, 210))  # abstract print for the picture frames
    draw = ImageDraw.Draw(art)
    for colour, box in [((196, 98, 62), (30, 40, 150, 190)), ((58, 92, 110), (110, 120, 226, 280)), ((222, 178, 92), (60, 170, 140, 260))]:
        draw.ellipse(box, fill=colour)
    art.filter(ImageFilter.GaussianBlur(1.2)).save(out / "art_print.png")

    return {name: {"source": "generated by robot/simulation/apartment.py", "license": "project-owned",
                   "texture": str((out / f"{name}.png").relative_to(ROOT)), "tile_m": 1.0}
            for name in ("shirt_red", "shirt_teal", "trousers_navy", "trousers_grey", "linen", "window_view", "art_print")}


def load_manifest():
    if not MANIFEST.is_file():
        raise SystemExit(f"{MANIFEST.relative_to(ROOT)} missing: run `apartment.py --fetch` first")
    return json.loads(MANIFEST.read_text())


# --------------------------------------------------------------------------- MJCF helpers

def _fmt(value):
    if isinstance(value, (tuple, list)):
        return " ".join(_fmt(v) for v in value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value else "0"
    return str(value)


def _el(parent, tag, **attrs):
    node = ET.SubElement(parent, tag)
    for key, value in attrs.items():
        if value is not None:
            node.set(key.rstrip("_"), _fmt(value))
    return node


def _yaw_quat(yaw_deg):
    half = math.radians(yaw_deg) / 2
    return (math.cos(half), 0.0, 0.0, math.sin(half))


class Scene:
    """Thin builder over the MJCF tree: materials once, then geoms."""

    def __init__(self, manifest, variant):
        self.manifest = manifest
        self.root = ET.Element("mujoco", model=f"annie_apartment_{variant}")
        xml_dir = OUT_DIR.resolve()
        _el(self.root, "include", file=os.path.relpath(GO2_DIR / "go2.xml", xml_dir))
        # After the include so these directories win over go2.xml's own meshdir="assets".
        self.meshdir = (GO2_DIR / "assets").resolve()
        _el(self.root, "compiler", angle="radian", autolimits="true", meshdir=os.path.relpath(self.meshdir, xml_dir),
            texturedir=os.path.relpath(TEXTURE_DIR.resolve(), xml_dir))
        _el(self.root, "statistic", center=(0, 0, 1.0), extent=4.0)
        visual = _el(self.root, "visual")
        _el(visual, "headlight", ambient=(0.46, 0.43, 0.39), diffuse=(0.12, 0.12, 0.11), specular=(0, 0, 0))
        _el(visual, "global", offwidth=1280, offheight=960, fovy=58)
        _el(visual, "quality", shadowsize=8192, offsamples=8)
        _el(visual, "map", znear=0.0125, zfar=12.5, shadowclip=1.0, shadowscale=0.8)
        self.asset = _el(self.root, "asset")
        self.world = _el(self.root, "worldbody")
        _el(self.asset, "texture", name="apt_sky", type="skybox", builtin="gradient", rgb1=(0.74, 0.83, 0.93),
            rgb2=(0.98, 0.95, 0.88), width=256, height=1536)
        self._materials, self._meshes = set(), set()

    def textured(self, key, *, tint=(1, 1, 1, 1), cube=False, **props):
        """Material backed by a manifest texture; returns its name."""
        name = "apt_" + key + ("_cube" if cube else "")
        if name not in self._materials:
            info = self.manifest["textures"][key]
            file = os.path.relpath(ROOT / info["texture"], TEXTURE_DIR.resolve())
            _el(self.asset, "texture", name=name, type="cube" if cube else "2d", file=file)
            repeat = 1.0 / info["tile_m"]
            _el(self.asset, "material", name=name, texture=name, rgba=tint, texuniform=None if cube else "true",
                texrepeat=None if cube else (repeat, repeat), **props)
            self._materials.add(name)
        return name

    def flat(self, name, rgba, **props):
        name = "apt_" + name
        if name not in self._materials:
            _el(self.asset, "material", name=name, rgba=rgba, **props)
            self._materials.add(name)
        return name

    def box(self, name, pos, size, material, *, collide=True, **attrs):
        return _el(self.world, "geom", name="env_" + name, type="box", pos=pos, size=size, material=material,
                   contype=None if collide else 0, conaffinity=None if collide else 0, **attrs)

    def panel(self, name, centre, normal, width, height, material, *, thickness=0.02, collide=False):
        """Vertical textured slab. Local z is the wall normal, so MuJoCo's xy texture projection is not streaked."""
        nx, ny = normal
        along = (-ny, nx, 0)  # horizontal direction along the wall
        return _el(self.world, "geom", name="env_" + name, type="box", pos=centre,
                   size=(width / 2, height / 2, thickness / 2), xyaxes=(*along, 0, 0, 1), material=material,
                   contype=None if collide else 0, conaffinity=None if collide else 0)

    def capsule(self, name, a, b, radius, material):
        return _el(self.world, "geom", name="env_" + name, type="capsule", fromto=(*a, *b), size=(radius,),
                   material=material, contype=0, conaffinity=0)

    def sphere(self, name, pos, radius, material):
        return _el(self.world, "geom", name="env_" + name, type="sphere", pos=pos, size=(radius,), material=material,
                   contype=0, conaffinity=0)

    def cylinder(self, name, a, b, radius, material):
        return _el(self.world, "geom", name="env_" + name, type="cylinder", fromto=(*a, *b), size=(radius,),
                   material=material, contype=0, conaffinity=0)

    def model(self, key, name, pos, yaw_deg=0.0, proxy=None):
        """Place a converted Poly Haven model (visual only) with an optional hidden box collision proxy."""
        info = self.manifest["models"][key]
        for i, part in enumerate(info["parts"]):
            mesh = f"apt_{key}_{i}"
            if mesh not in self._meshes:
                _el(self.asset, "mesh", name=mesh, file=os.path.relpath(ROOT / part["mesh"], self.meshdir), inertia="shell")
                if part.get("texture"):
                    _el(self.asset, "texture", name=mesh, type="2d", file=os.path.relpath(ROOT / part["texture"], TEXTURE_DIR.resolve()))
                    _el(self.asset, "material", name=mesh, texture=mesh, specular=0.12, shininess=0.25)
                else:
                    _el(self.asset, "material", name=mesh, rgba=part.get("rgba") or (0.6, 0.6, 0.6, 1), specular=0.2)
                self._meshes.add(mesh)
            _el(self.world, "geom", name=f"env_{name}_{i}", type="mesh", mesh=mesh, material=mesh, pos=pos,
                quat=_yaw_quat(yaw_deg), contype=0, conaffinity=0, group=2)
        if proxy:
            sx, sy, sz = proxy
            _el(self.world, "geom", name=f"env_{name}_collision", type="box", pos=(pos[0], pos[1], pos[2] + sz / 2),
                size=(sx / 2, sy / 2, sz / 2), quat=_yaw_quat(yaw_deg), rgba=(0, 0, 0, 0), group=3)
        return info["size_m"]


# --------------------------------------------------------------------------- people

POSES = {
    # Local frame: +x is the way the person faces (standing/sitting) or where the head points (lying),
    # +y is their left, +z is up. Metres for a 1.72 m adult; scaled by height.
    "standing": {
        "ankle": (0, 0.10, 0.08), "knee": (0.02, 0.10, 0.49), "hip": (0, 0.095, 0.90), "pelvis": (0, 0, 0.95),
        "chest": (0, 0, 1.32), "shoulder": (0, 0.19, 1.40), "neck": (0, 0, 1.46), "head": (0.01, 0, 1.60),
        "elbow_l": (0.02, 0.235, 1.13), "wrist_l": (0.26, 0.17, 1.17), "elbow_r": (0.0, -0.235, 1.13),
        "wrist_r": (0.06, -0.225, 0.89), "toe": (0.15, 0, 0), "face": (1, 0, 0), "crown": (0, 0, 1),
    },
    "sitting": {  # z values are relative to the seat height, added in person()
        "ankle": (0.50, 0.11, 0.08), "knee": (0.43, 0.11, 0.13), "hip": (0, 0.095, 0.10), "pelvis": (0, 0, 0.12),
        "chest": (-0.10, 0, 0.47), "shoulder": (-0.11, 0.19, 0.55), "neck": (-0.11, 0, 0.61), "head": (-0.09, 0, 0.75),
        "elbow_l": (-0.02, 0.25, 0.30), "wrist_l": (0.26, 0.13, 0.22), "elbow_r": (-0.02, -0.25, 0.30),
        "wrist_r": (0.26, -0.13, 0.22), "toe": (0.15, 0, 0), "face": (1, 0, 0), "crown": (0, 0, 1),
    },
    "lying": {  # supine on the floor, origin under the pelvis
        "ankle": (-0.82, 0.15, 0.06), "knee": (-0.42, 0.12, 0.09), "hip": (0, 0.095, 0.11), "pelvis": (0, 0, 0.12),
        "chest": (0.37, 0, 0.13), "shoulder": (0.45, 0.19, 0.12), "neck": (0.51, 0, 0.12), "head": (0.66, 0, 0.115),
        "elbow_l": (0.18, 0.27, 0.05), "wrist_l": (-0.06, 0.30, 0.04), "elbow_r": (0.40, -0.46, 0.05),
        "wrist_r": (0.62, -0.62, 0.04), "toe": (0, 0, 0.14), "face": (0, 0, 1), "crown": (1, 0, 0),
        "knee_l": (-0.36, 0.30, 0.10), "ankle_l": (-0.70, 0.12, 0.06),
    },
}


def person(scene, name, posture, origin, yaw_deg, *, height=1.72, shirt, trousers, hair, skin, shoes, seat_height=0.0, mug=False):
    """Capsule mannequin in a fixed pose. `origin` is the floor point under the pelvis."""
    pose, s = POSES[posture], height / 1.72
    cos, sin = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    lift = seat_height if posture == "sitting" else 0.0

    def world(p, side=1, relative=False):
        x, y, z = p[0] * s, p[1] * side * s, p[2] * s
        if relative:
            return (x * cos - y * sin, x * sin + y * cos, z)
        if posture == "sitting" and p is not pose["ankle"]:
            z += lift
        return (origin[0] + x * cos - y * sin, origin[1] + x * sin + y * cos, z)

    def add(a, b):
        return tuple(a[i] + b[i] for i in range(3))

    def mul(v, k):
        return tuple(c * k for c in v)

    left = world((0, 1, 0), relative=True)
    face, crown = world(pose["face"], relative=True), world(pose["crown"], relative=True)
    n = "person_" + name + "_"
    for label, side in (("l", 1), ("r", -1)):
        hip = world(pose["hip"], side)
        knee = world(pose.get("knee_" + label, pose["knee"]), 1 if "knee_" + label in pose else side)
        ankle = world(pose.get("ankle_" + label, pose["ankle"]), 1 if "ankle_" + label in pose else side)
        scene.capsule(n + "thigh_" + label, hip, knee, 0.078 * s, trousers)
        scene.capsule(n + "shin_" + label, knee, ankle, 0.056 * s, trousers)
        scene.capsule(n + "foot_" + label, ankle, add(ankle, world(pose["toe"], relative=True)), 0.046 * s, shoes)
        shoulder, elbow, wrist = world(pose["shoulder"], side), world(pose["elbow_" + label]), world(pose["wrist_" + label])
        scene.capsule(n + "upper_arm_" + label, shoulder, elbow, 0.052 * s, shirt)
        scene.capsule(n + "forearm_" + label, elbow, wrist, 0.043 * s, shirt)
        scene.sphere(n + "hand_" + label, wrist, 0.046 * s, skin)
        pelvis, chest = world(pose["pelvis"]), world(pose["chest"])
        offset = mul(left, 0.058 * s * side)
        scene.capsule(n + "torso_" + label, add(pelvis, offset), add(chest, offset), 0.128 * s, shirt)
    scene.capsule(n + "shoulders", world(pose["shoulder"], 0.8), world(pose["shoulder"], -0.8), 0.088 * s, shirt)
    scene.capsule(n + "hips", world(pose["hip"], 0.9), world(pose["hip"], -0.9), 0.118 * s, trousers)
    head = world(pose["head"])
    scene.capsule(n + "neck", world(pose["neck"]), head, 0.046 * s, skin)
    scene.sphere(n + "head", head, 0.102 * s, skin)
    scene.sphere(n + "hair", add(add(head, mul(face, -0.05 * s)), mul(crown, 0.04 * s)), 0.1 * s, hair)
    scene.sphere(n + "bun", add(add(head, mul(face, -0.105 * s)), mul(crown, 0.06 * s)), 0.05 * s, hair)
    dark = scene.flat("eye", (0.10, 0.08, 0.08, 1))
    for label, side in (("l", 1), ("r", -1)):
        eye = add(add(add(head, mul(face, 0.09 * s)), mul(left, 0.036 * s * side)), mul(crown, 0.018 * s))
        scene.sphere(n + "eye_" + label, eye, 0.013 * s, dark)
    scene.sphere(n + "nose", add(add(head, mul(face, 0.10 * s)), mul(crown, -0.012 * s)), 0.017 * s, skin)
    if mug:
        hand = world(pose["wrist_l"])
        scene.cylinder(n + "mug", add(hand, (0, 0, 0.02)), add(hand, (0, 0, 0.12)), 0.04, scene.flat("mug", (0.95, 0.94, 0.90, 1), specular=0.5))


# --------------------------------------------------------------------------- the flat

def build(variant="seated"):
    """Return the MJCF string for one variant: 'seated' (Jeanine on the couch + visitor), 'floor', or 'empty'."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    sc = Scene(load_manifest(), variant)
    W, D, H = 3.5, 2.5, 2.5  # half-width (x), half-depth (y), wall height

    plaster = sc.textured("plaster", tint=(0.97, 0.93, 0.86, 1), specular=0.02)
    parquet = sc.textured("parquet", specular=0.25, shininess=0.4, reflectance=0.10)
    tiles = sc.textured("tiles", specular=0.3, shininess=0.5, reflectance=0.12)
    oak = sc.textured("oak", specular=0.15, shininess=0.3)
    marble = sc.textured("marble", specular=0.5, shininess=0.7, reflectance=0.05)
    backsplash = sc.textured("backsplash", specular=0.4, shininess=0.6)
    white = sc.flat("white_paint", (0.95, 0.94, 0.91, 1), specular=0.1)
    steel = sc.flat("steel", (0.72, 0.73, 0.74, 1), specular=0.8, shininess=0.8)
    black = sc.flat("black_glass", (0.04, 0.04, 0.05, 1), specular=0.9, shininess=0.9)

    # Floors: parquet everywhere (plane, so it reflects), tile inlay under the kitchen, hallway beyond the door.
    # Side by side, not an inlay: coplanar or near-coplanar surfaces z-fight from the dog's grazing view.
    _el(sc.world, "geom", name="env_floor", type="plane", pos=(-1.3, 0, 0), size=(3.5, D + 0.2, 0.1), material=parquet)
    _el(sc.world, "geom", name="env_floor_kitchen", type="plane", pos=(2.85, 0, 0), size=(0.65, D + 0.2, 0.1), material=tiles)
    sc.box("floor_threshold", (2.2, 0, 0.004), (0.02, D, 0.004), steel, collide=False)

    # Walls. West wall has the entry doorway (y -1.9..-1.0, 2.05 m high) into a short hallway.
    sc.panel("wall_north", (0, D, H / 2), (0, -1), 2 * W, H, plaster, thickness=0.1, collide=True)
    sc.panel("wall_south", (0, -D, H / 2), (0, 1), 2 * W, H, plaster, thickness=0.1, collide=True)
    sc.panel("wall_east", (W, 0, H / 2), (-1, 0), 2 * D, H, plaster, thickness=0.1, collide=True)
    sc.panel("wall_west_a", (-W, 0.75, H / 2), (1, 0), 3.5, H, plaster, thickness=0.1, collide=True)      # y -1.0..2.5
    sc.panel("wall_west_b", (-W, -2.2, H / 2), (1, 0), 0.6, H, plaster, thickness=0.1, collide=True)      # y -2.5..-1.9
    sc.panel("wall_west_lintel", (-W, -1.45, 2.275), (1, 0), 0.9, 0.45, plaster, thickness=0.1, collide=True)
    sc.panel("hall_north", (-4.15, -0.85, H / 2), (0, -1), 1.3, H, plaster, thickness=0.1, collide=True)
    sc.panel("hall_south", (-4.15, -2.05, H / 2), (0, 1), 1.3, H, plaster, thickness=0.1, collide=True)
    sc.panel("hall_end", (-4.8, -1.45, H / 2), (1, 0), 1.3, H, plaster, thickness=0.1, collide=True)
    sc.box("ceiling", (-0.6, 0, H + 0.03), (W + 0.8, D + 0.1, 0.03), white, collide=False, group=2)
    for label, y in (("s", -1.92), ("n", -0.98)):  # door frame
        sc.box("doorframe_" + label, (-W, y, 1.025), (0.07, 0.035, 1.025), white, collide=False)
    sc.box("doorframe_top", (-W, -1.45, 2.085), (0.07, 0.505, 0.035), white, collide=False)
    sc.panel("door_leaf", (-3.93, -0.93, 1.01), (0, -1), 0.86, 2.02, oak, thickness=0.04)  # swung open into the hall
    sc.capsule("door_handle", (-4.22, -0.97, 1.0), (-4.10, -0.97, 1.0), 0.012, steel)
    for label, pos, size in [("n", (0, D - 0.06, 0.05), (W, 0.012, 0.05)), ("s", (0, -D + 0.06, 0.05), (W, 0.012, 0.05)),
                             ("e", (W - 0.06, 0, 0.05), (0.012, D, 0.05)), ("w", (-W + 0.06, 0.75, 0.05), (0.012, 1.75, 0.05))]:
        sc.box("skirting_" + label, pos, size, white, collide=False)

    # Window over the couch: bright pane, frame, mullions, curtains.
    sc.panel("window_view", (-0.9, D - 0.075, 1.55), (0, -1), 2.0, 1.2, sc.textured_emissive("window_view"), thickness=0.004)
    for label, pos, size in [("top", (-0.9, D - 0.1, 2.18), (1.06, 0.05, 0.035)), ("bottom", (-0.9, D - 0.12, 0.93), (1.1, 0.07, 0.03)),
                             ("l", (-1.93, D - 0.1, 1.55), (0.035, 0.05, 0.66)), ("r", (0.13, D - 0.1, 1.55), (0.035, 0.05, 0.66)),
                             ("mid", (-0.9, D - 0.115, 1.55), (0.02, 0.02, 0.62)), ("bar", (-0.9, D - 0.115, 1.55), (1.0, 0.02, 0.018))]:
        sc.box("window_" + label, pos, size, white, collide=False)
    curtain = sc.textured("linen", cube=True, specular=0.02)
    for label, x in (("l", -2.2), ("r", 0.4)):
        for fold in range(4):
            sc.capsule(f"curtain_{label}_{fold}", (x + 0.09 * fold - 0.13, D - 0.2, 0.12), (x + 0.09 * fold - 0.13, D - 0.2, 2.25), 0.055, curtain)
    sc.capsule("curtain_rail", (-2.5, D - 0.2, 2.3), (0.7, D - 0.2, 2.3), 0.012, steel)

    # Living area.
    sofa = sc.model("sofa", "sofa", (-0.9, 1.92, 0), 0, proxy=(2.0, 0.9, 0.45))
    sc.box("rug", (-0.9, 0.75, 0.01), (1.35, 0.95, 0.01), sc.textured("rug", tint=(0.78, 0.74, 0.70, 1), specular=0.0), collide=False)
    sc.box("rug_border", (-0.9, 0.75, 0.008), (1.41, 1.01, 0.008), sc.flat("rug_border", (0.36, 0.40, 0.46, 1)), collide=False)
    sc.model("coffee_table", "coffee_table", (-0.3, 0.85, 0.02), 90, proxy=(0.6, 1.2, 0.38))
    sc.model("side_table", "side_table", (0.85, 2.08, 0), 0, proxy=(0.55, 0.45, 0.55))
    side = sc.manifest["models"]["side_table"]["size_m"][2]
    sc.model("plant", "plant", (0.85, 2.08, side), 25)
    sc.model("cabinet", "cabinet", (-3.12, 0.9, 0), 90, proxy=(1.2, 0.62, 1.18))
    cabinet = sc.manifest["models"]["cabinet"]["size_m"][2]
    sc.model("vase", "vase", (-3.12, 0.55, cabinet), 0)
    sc.model("frame_a", "frame_a", (-3.42, 0.9, 1.75), 90)
    sc.model("frame_b", "frame_b", (1.5, D - 0.075, 1.35), 0)
    sc.model("clock", "clock", (W - 0.07, 1.6, 1.75), -90)
    # Floor lamp: weighted base, pole, warm emissive shade.
    sc.cylinder("lamp_base", (-2.62, 2.1, 0), (-2.62, 2.1, 0.03), 0.16, black)
    sc.capsule("lamp_pole", (-2.62, 2.1, 0.03), (-2.62, 2.1, 1.45), 0.013, steel)
    sc.cylinder("lamp_shade", (-2.62, 2.1, 1.38), (-2.62, 2.1, 1.68), 0.19,
                sc.flat("lamp_shade", (1.0, 0.86, 0.62, 1), emission=0.9))
    # Low sideboard + dark TV on the south wall.
    sc.box("sideboard", (-0.9, -2.22, 0.27), (0.8, 0.22, 0.25), oak)
    for leg_x in (-1.6, -0.2):
        sc.box(f"sideboard_leg_{leg_x}", (leg_x, -2.22, 0.01), (0.03, 0.18, 0.01), black, collide=False)
    sc.panel("tv", (-0.9, -2.41, 1.05), (0, 1), 1.15, 0.66, black, thickness=0.03)

    # Dining area: table, three chairs, pendant lamp.
    sc.model("dining_table", "dining_table", (1.55, 0.75, 0), 0, proxy=(1.0, 0.7, 0.74))
    for label, pos, yaw in [("n", (1.55, 1.42, 0), 0), ("s", (1.55, 0.08, 0), 180), ("w", (0.78, 0.75, 0), 90)]:
        sc.model("dining_chair", "dining_chair_" + label, pos, yaw, proxy=(0.45, 0.45, 0.45))
    pendant = sc.manifest["models"]["pendant"]["size_m"][2]
    sc.model("pendant", "pendant", (1.55, 0.75, H - pendant), 0)

    # Kitchen run on the east wall: base cabinets, marble top, tile backsplash, wall cabinets, hob, sink, fridge.
    front_x, run = W - 0.62, [(-1.55, 0.58), (-0.95, 0.58), (-0.35, 0.58), (0.25, 0.58)]
    sc.box("counter_carcass", (W - 0.33, -0.65, 0.45), (0.28, 1.22, 0.43), white)
    for i, (y, width) in enumerate(run):
        sc.panel(f"counter_door_{i}", (front_x, y, 0.47), (-1, 0), width, 0.78, oak, thickness=0.02)
        sc.capsule(f"counter_handle_{i}", (front_x - 0.03, y - 0.1, 0.8), (front_x - 0.03, y + 0.1, 0.8), 0.008, steel)
    sc.box("countertop", (W - 0.35, -0.65, 0.9), (0.33, 1.25, 0.02), marble)
    sc.panel("backsplash", (W - 0.07, -0.65, 1.19), (-1, 0), 2.5, 0.54, backsplash, thickness=0.02)
    for i, (y, width) in enumerate(run):
        sc.panel(f"upper_door_{i}", (W - 0.37, y, 1.8), (-1, 0), width, 0.68, oak, thickness=0.02)
    sc.box("upper_carcass", (W - 0.2, -0.65, 1.8), (0.16, 1.22, 0.35), white, collide=False)
    sc.box("hob", (W - 0.36, -0.9, 0.924), (0.25, 0.29, 0.008), black, collide=False)
    sc.box("sink", (W - 0.36, 0.0, 0.924), (0.2, 0.25, 0.008), steel, collide=False)
    sc.capsule("tap_riser", (W - 0.14, 0.0, 0.92), (W - 0.14, 0.0, 1.16), 0.012, steel)
    sc.capsule("tap_spout", (W - 0.14, 0.0, 1.16), (W - 0.30, 0.0, 1.14), 0.011, steel)
    sc.box("fridge", (W - 0.36, -2.1, 0.92), (0.33, 0.33, 0.92), steel)
    sc.capsule("fridge_handle", (W - 0.71, -1.85, 0.75), (W - 0.71, -1.85, 1.45), 0.012, black)
    # Closed interior door on the north wall, by the kitchen.
    sc.panel("door_north", (2.75, D - 0.065, 1.01), (0, -1), 0.86, 2.02, oak, thickness=0.04)
    for label, pos, size in [("l", (2.29, D - 0.07, 1.03), (0.035, 0.03, 1.03)), ("r", (3.21, D - 0.07, 1.03), (0.035, 0.03, 1.03)),
                             ("top", (2.75, D - 0.07, 2.075), (0.495, 0.03, 0.035))]:
        sc.box("door_north_frame_" + label, pos, size, white, collide=False)
    sc.capsule("door_north_handle", (3.02, D - 0.13, 1.0), (3.12, D - 0.13, 1.0), 0.012, steel)

    # People.
    skin = sc.flat("skin", (0.86, 0.66, 0.55, 1), specular=0.08)
    grandma = dict(height=1.60, shirt=sc.textured("shirt_red", cube=True, emission=0.12), hair=sc.flat("hair_grey", (0.80, 0.79, 0.77, 1)),
                   trousers=sc.textured("trousers_grey", cube=True), skin=skin, shoes=sc.flat("slippers", (0.45, 0.30, 0.34, 1)))
    if variant == "seated":
        person(sc, "jeanine", "sitting", (-1.45, 1.78, 0), -90, seat_height=0.40, **grandma)
        person(sc, "visitor", "standing", (0.55, -0.45, 0), 196, height=1.78, mug=True,
               shirt=sc.textured("shirt_teal", cube=True), trousers=sc.textured("trousers_navy", cube=True),
               hair=sc.flat("hair_dark", (0.16, 0.12, 0.10, 1)), skin=sc.flat("skin_b", (0.74, 0.55, 0.43, 1), specular=0.08),
               shoes=sc.flat("shoes", (0.20, 0.17, 0.15, 1)))
    elif variant == "floor":
        person(sc, "jeanine", "lying", (-1.3, 0.1, 0), 8, **grandma)
        sc.cylinder("dropped_mug", (-0.95, -0.62, 0.04), (-0.86, -0.66, 0.04), 0.04, sc.flat("mug", (0.95, 0.94, 0.90, 1), specular=0.5))

    # Spot cutoffs stay <= 90 deg (or exactly 180): OpenGL rejects anything between, and cos^exponent of an
    # angle past 90 deg is NaN, which rendered the ceiling and upper walls pure black.
    # Lighting: warm shadow-casting key over the living area, pendant pool over the table, lamp glow, cool window fill.
    _el(sc.world, "light", name="env_key", pos=(-0.9, 0.2, 2.36), dir=(0.05, 0.1, -1), cutoff=90, exponent=0.4,
        diffuse=(0.80, 0.70, 0.56), specular=(0.18, 0.16, 0.12), attenuation=(0.65, 0.04, 0.015), castshadow="true")
    # Stand-ins for bounced light (MuJoCo has no global illumination): an up-light for the ceiling, a soft fill
    # from behind the dog's usual viewpoint. Neither casts shadows.
    _el(sc.world, "light", name="env_ceiling_bounce", pos=(0, 0, 0.5), dir=(0, 0, 1), directional="true",
        diffuse=(0.40, 0.38, 0.34), specular=(0, 0, 0), castshadow="false")
    _el(sc.world, "light", name="env_room_fill", pos=(-3, -2, 1.5), dir=(0.45, 0.8, -0.25), directional="true",
        diffuse=(0.24, 0.22, 0.19), specular=(0, 0, 0), castshadow="false")
    _el(sc.world, "light", name="env_pendant", pos=(1.55, 0.75, H - pendant - 0.02), dir=(0, 0, -1), cutoff=70, exponent=3,
        diffuse=(0.62, 0.50, 0.34), specular=(0.1, 0.1, 0.08), attenuation=(0.6, 0.1, 0.06), castshadow="true")
    _el(sc.world, "light", name="env_floor_lamp", pos=(-2.62, 1.95, 1.5), cutoff=180, diffuse=(0.50, 0.38, 0.22), specular=(0, 0, 0),
        attenuation=(0.5, 0.25, 0.2), castshadow="false")
    _el(sc.world, "light", name="env_window_fill", pos=(-0.9, 2.2, 1.6), dir=(0.1, -1, -0.35), directional="true",
        diffuse=(0.30, 0.34, 0.40), specular=(0.05, 0.05, 0.06), castshadow="false")
    _el(sc.world, "light", name="env_hall", pos=(-4.15, -1.45, 2.4), dir=(0, 0, -1), cutoff=75, exponent=2,
        diffuse=(0.45, 0.40, 0.32), specular=(0, 0, 0), attenuation=(0.7, 0.1, 0.05), castshadow="false")
    _el(sc.world, "camera", name="overview", pos=(-3.1, -2.2, 2.05), xyaxes=(0.62, -0.78, 0, 0.27, 0.215, 0.94), fovy=68)

    x, y, yaw = PATHS[variant][0]
    home = "0 0.9 -1.8 " * 4
    keyframe = _el(sc.root, "keyframe")
    _el(keyframe, "key", name="apartment_start", qpos=f"{x} {y} 0.27 {_fmt(_yaw_quat(yaw))} {home}".strip(),
        ctrl=home.strip())
    ET.indent(sc.root, space="  ")
    header = ("<!-- Generated by robot/simulation/apartment.py; edit the builder, not this file.\n"
              "     Needs `apartment.py --fetch` (CC0 Poly Haven cache) and the pinned Go2 model. Sources: SOURCES.md -->\n")
    return header + ET.tostring(sc.root, encoding="unicode") + "\n"


def _textured_emissive(self, key):
    return self.textured(key, emission=1.0, specular=0)


Scene.textured_emissive = _textured_emissive


def write_all():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for variant in VARIANTS:
        path = OUT_DIR / f"apartment_{variant}.xml"
        path.write_text(build(variant))
        paths.append(path)
    return paths


def load_model(variant="seated"):
    """Compile a variant with the dog's `robot_front` camera attached to the Go2 base (640x480 forward view)."""
    import mujoco

    path = OUT_DIR / f"apartment_{variant}.xml"
    if not path.is_file():
        raise SystemExit(f"{path.relative_to(ROOT)} missing: run `apartment.py --build` first")
    spec = mujoco.MjSpec.from_file(str(path))
    spec.body("base").add_camera(name=CAMERA["name"], pos=list(CAMERA["pos"]), xyaxes=list(CAMERA["xyaxes"]), fovy=CAMERA["fovy"])
    return spec.compile()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch", action="store_true", help="download/convert CC0 assets into .cache (network)")
    parser.add_argument("--build", action="store_true", help="write assets/apartment/apartment_*.xml (no network)")
    args = parser.parse_args()
    if not (args.fetch or args.build):
        parser.error("choose --fetch and/or --build")
    if args.fetch:
        prepare_assets()
    if args.build:
        import mujoco

        for path in write_all():
            model = mujoco.MjModel.from_xml_path(str(path))
            print(json.dumps({"scene": str(path.relative_to(ROOT)), "geoms": model.ngeom, "meshes": model.nmesh, "lights": model.nlight}))


if __name__ == "__main__":
    main()
