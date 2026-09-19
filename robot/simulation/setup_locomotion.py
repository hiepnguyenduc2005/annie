"""One-command bootstrap for cached Go1 locomotion assets (fresh checkouts).

Downloads the pinned DimOS mujoco_sim LFS archive once, extracts ONLY the two
hashed Go1 files, and prepares the pinned sparse Menagerie Go1 mesh checkout.
Everything lands in ignored cache; no binaries are committed. Reruns print
ready and skip verified files; nothing is re-downloaded while hashes match.

    python robot/simulation/setup_locomotion.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Pinned DimOS revision whose released mujoco_sim data assets we verify.
# The media URL at this revision serves the real ~60 MB gzip blob, not an LFS
# pointer (checked 2026-09-19). Contents are hash-verified on extraction.
DIMOS_COMMIT = "c1c3cdc9d2ee54ca72259465688395699d7d99a2"
ARCHIVE_URL = (
    "https://media.githubusercontent.com/media/dimensionalOS/dimos/"
    + DIMOS_COMMIT
    + "/data/.lfs/mujoco_sim.tar.gz"
)
ARCHIVE_MAX_BYTES = 100 * 1024 * 1024

DATA_DIR = ROOT / ".cache/dimos/data/mujoco_sim"
MODEL_PATH = DATA_DIR / "unitree_go1.xml"
POLICY_PATH = DATA_DIR / "unitree_go1_policy.onnx"
EXPECTED = {
    MODEL_PATH: "97058b2d17ee311cc7eddcd33e87524717533916fb597448b3c8745875c012e0",
    POLICY_PATH: "386cde6a1eac679e2f2a313ade2b395d9f26905b151ee3b019c2c4163d49b6f2",
}
# Reuse an archive already fetched by other tooling before downloading.
ARCHIVE_CANDIDATES = (
    ROOT / ".cache/dimos/data/.lfs/mujoco_sim.tar.gz",
    ROOT / ".cache/dimos-archive/mujoco_sim.tar.gz",
)

MENAGERIE_DIR = ROOT / ".cache/menagerie_full"
MENAGERIE_COMMIT = "8161bba264d7fa7c99ca301e91e7fb44737676ad"
MENAGERIE_URL = "https://github.com/google-deepmind/mujoco_menagerie.git"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_targets() -> dict[Path, bool]:
    return {
        path: path.is_file() and sha256(path) == expected
        for path, expected in EXPECTED.items()
    }


def download_archive(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        ARCHIVE_URL, headers={"User-Agent": "Annie-local-setup/1.0"}
    )
    used = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        fd, temp_name = tempfile.mkstemp(
            dir=dest.parent, prefix=".mujoco_sim-", suffix=".tar.gz"
        )
        with os.fdopen(fd, "wb") as stream:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                used += len(chunk)
                if used > ARCHIVE_MAX_BYTES:
                    raise ValueError(
                        "Download exceeded %d byte cap" % ARCHIVE_MAX_BYTES
                    )
                stream.write(chunk)
    Path(temp_name).replace(dest)


def extract_members(archive: Path) -> None:
    """Scan members read-only; extract only the two hashed Go1 files."""
    targets = {path.name for path in EXPECTED}
    extracted = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = Path(member.name).name
            if name not in targets or name in extracted:
                continue
            dest = DATA_DIR / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, open(dest, "wb") as out:
                shutil.copyfileobj(source, out)
            extracted.add(name)
    missing = targets - extracted
    if missing:
        raise ValueError("Archive lacks required members: %s" % sorted(missing))


def ensure_menagerie() -> None:
    """Prepare the pinned sparse Go1 checkout; never move an existing one."""
    assets = MENAGERIE_DIR / "unitree_go1" / "assets"
    if MENAGERIE_DIR.exists():
        probe = subprocess.run(
            ["git", "-C", str(MENAGERIE_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        head = probe.stdout.strip()
        if probe.returncode != 0:
            raise SystemExit(
                "%s exists but is not a usable git checkout;"
                " remove or repair it manually." % MENAGERIE_DIR
            )
        if head != MENAGERIE_COMMIT:
            raise SystemExit(
                "%s is at %s but this repo pins %s; fix it manually"
                " (this tool never mutates an existing checkout)."
                % (MENAGERIE_DIR, head or "unknown", MENAGERIE_COMMIT)
            )
    else:
        MENAGERIE_DIR.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop("GIT_CONFIG_GLOBAL", None)
        print("Cloning %s (sparse, %s)" % (MENAGERIE_URL, MENAGERIE_COMMIT[:12]))
        subprocess.run(
            [
                "git", "clone", "--filter=blob:none", "--sparse",
                MENAGERIE_URL, str(MENAGERIE_DIR),
            ],
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(MENAGERIE_DIR), "sparse-checkout", "set", "unitree_go1"],
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(MENAGERIE_DIR), "checkout", MENAGERIE_COMMIT],
            check=True,
            env=env,
        )
    if not assets.is_dir() or not any(assets.glob("*.stl")):
        raise SystemExit("Missing Go1 meshes after checkout: %s" % assets)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()
    status = check_targets()
    skipped = True
    for path in EXPECTED:
        ok = status[path]
        skipped = skipped and ok
        print(("ok" if ok else "MISSING/INVALID") + "  " + str(path.relative_to(ROOT)))
    if not skipped:
        archive = next((a for a in ARCHIVE_CANDIDATES if a.is_file()), None)
        if archive is None:
            archive = ARCHIVE_CANDIDATES[0]
            print("Downloading " + ARCHIVE_URL)
            download_archive(archive)
        else:
            print("Using cached archive " + str(archive))
        extract_members(archive)
        status = check_targets()
        for path in EXPECTED:
            if not status[path]:
                raise SystemExit("Hash verification failed: %s" % path)
    ensure_menagerie()
    if skipped:
        print("Ready: cached locomotion assets already verified; skipped.")
    else:
        print("Ready: locomotion assets verified.")


if __name__ == "__main__":
    main()
