"""Fetch bounded CC0 Poly Haven assets and prepare textured MJCF-compatible OBJ files.

Run explicitly with the simulation Python environment. Scene generation itself never
uses the network. Existing DimOS person data is reused without downloading it.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache/simulation-assets"
LIMIT = 200 * 1024 * 1024


def prepare():
    import numpy as np
    import trimesh

    CACHE.mkdir(parents=True, exist_ok=True)
    records = []
    used = 0

    def fetch(url, path):
        nonlocal used
        if not path.resolve().is_relative_to(CACHE.resolve()):
            raise ValueError("Asset path escapes cache")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "api.polyhaven.com",
            "dl.polyhaven.org",
        }:
            raise ValueError("Unsupported asset source URL")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            request = urllib.request.Request(
                url, headers={"User-Agent": "Annie-local-simulator/1.0"}
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read(LIMIT - used + 1)
            used += len(data)
            if used > LIMIT:
                raise ValueError("Asset download budget exceeded")
            path.write_bytes(data)
        records.append(
            {
                "url": url,
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
        return path

    converted = {}
    for key, ident, height in [
        ("chair", "modern_arm_chair_01", 0.95),
        ("table", "small_wooden_table_01", 0.795),
    ]:
        base = CACHE / ident
        api = fetch("https://api.polyhaven.com/files/" + ident, base / "files.json")
        info = json.loads(api.read_text())["gltf"]["1k"]["gltf"]
        file = fetch(info["url"], base / (ident + ".gltf"))
        for name, item in info["include"].items():
            fetch(item["url"], base / name)
        scene = trimesh.load(file, force="scene")
        parts = []
        for node in scene.graph.nodes_geometry:
            transform, name = scene.graph[node]
            mesh = scene.geometry[name].copy()
            mesh.apply_transform(transform)
            mesh.apply_transform(
                trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])
            )
            parts.append(mesh)
        bounds = np.array([p.bounds for p in parts])
        low = bounds[:, 0].min(axis=0)
        high = bounds[:, 1].max(axis=0)
        scale = height / (high[2] - low[2])
        center = (low + high) / 2
        center[2] = low[2]
        entries = []
        for i, mesh in enumerate(parts):
            mesh.vertices = (mesh.vertices - center) * scale
            obj = base / f"mjcf_{i}.obj"
            texture = base / f"mjcf_{i}.png"
            image = getattr(mesh.visual.material, "baseColorTexture", None)
            if image is not None:
                image.save(texture)
            obj.write_text(trimesh.exchange.obj.export_obj(mesh, include_texture=True))
            entries.append(
                {
                    "mesh": str(obj.relative_to(ROOT)),
                    "texture": str(texture.relative_to(ROOT))
                    if image is not None
                    else None,
                }
            )
        converted[key] = {
            "source": "https://polyhaven.com/a/" + ident,
            "license": "CC0",
            "height_m": height,
            "parts": entries,
        }
    floor_api = fetch(
        "https://api.polyhaven.com/files/wood_floor", CACHE / "floor_files.json"
    )
    floor_info = json.loads(floor_api.read_text())["Diffuse"]["1k"]["jpg"]
    floor = fetch(floor_info["url"], CACHE / "wood_floor.jpg")
    from PIL import Image

    Image.open(floor).save(CACHE / "wood_floor.png")
    floor = CACHE / "wood_floor.png"
    converted["floor"] = {
        "source": "https://polyhaven.com/a/wood_floor",
        "license": "CC0",
        "texture": str(floor.relative_to(ROOT)),
    }
    person = ROOT / ".cache/dimos/data/person/jeong_seun_34.obj"
    if person.exists():
        mesh = trimesh.load(person, force="mesh", process=False)
        mesh.apply_transform(
            trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])
        )
        low, high = mesh.bounds
        center = (low + high) / 2
        center[2] = low[2]
        mesh.vertices = (mesh.vertices - center) * (1.72 / (high[2] - low[2]))
        dest = CACHE / "person.obj"
        dest.write_text(trimesh.exchange.obj.export_obj(mesh, include_texture=True))
        converted["person"] = {
            "source": "https://github.com/dimensionalOS/dimos/blob/main/dimos/simulation/mujoco/model.py",
            "license": "upstream DimOS asset; source-specific license not established",
            "height_m": 1.72,
            "parts": [
                {
                    "mesh": str(dest.relative_to(ROOT)),
                    "texture": ".cache/dimos/data/person/material_0.png",
                }
            ],
            "bounds": mesh.bounds.tolist(),
        }
        for path in (person, ROOT / ".cache/dimos/data/person/material_0.png"):
            records.append(
                {
                    "url": "https://media.githubusercontent.com/media/dimensionalOS/dimos/main/data/.lfs/person.tar.gz",
                    "path": str(path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
            )
    for path in sorted(CACHE.rglob("*")):
        if (
            path.is_file()
            and path.name != "manifest.json"
            and not any(r["path"] == str(path.relative_to(ROOT)) for r in records)
        ):
            records.append(
                {
                    "url": None,
                    "derived_from": "source files in this manifest",
                    "path": str(path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
            )
    manifest = {
        "version": 1,
        "credit": "Powered by Poly Haven",
        "assets": converted,
        "files": records,
    }
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "downloaded_bytes": used,
                "assets": list(converted),
                "manifest": str(CACHE / "manifest.json"),
            }
        )
    )


if __name__ == "__main__":
    prepare()
