"""Local face similarity matching for enrolled, consenting people.

``FaceIndex`` stores face embeddings per enrolled name and answers "which
enrolled person does the face inside this box look most like?" with a cosine
similarity. ``PersonTracker(face_index=...)`` uses it to attach
``identity: {name, score}`` to a track. Everything runs on this machine; no
image, embedding or name leaves it.

A match is a similarity estimate above a threshold, not an identification:
scores are raw cosine values, not calibrated probabilities, and look-alikes,
poor light, small or turned faces produce both misses and false matches.
Nothing here may gate safety behaviour or access on a name.

Consent: enrol only people who agreed to it, and never the resident by
default. Photos live under the git-ignored ``.data/faces/<name>/`` and the
index (biometric templates, written 0600) at ``.data/faces/index.json``;
``forget`` removes a person's templates. Embeddings and photos are never
logged.

Backend and provenance (forwarded for the separate compliance review):

- Code: ``insightface`` 2.0 from PyPI (https://github.com/deepinsight/insightface,
  MIT licence for the Python library) on ``onnxruntime`` CPU.
- Weights: InsightFace model pack ``buffalo_sc`` (SCRFD-500MF detector
  ``det_500m.onnx`` + MobileFaceNet ``w600k_mbf.onnx`` recogniser trained on
  WebFace600K), https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_sc.zip
  cached under the git-ignored ``.cache/insightface/models/``. Per the
  InsightFace model card the pretrained packs are for NON-COMMERCIAL RESEARCH
  USE ONLY; the library reports the same ``default_non_commercial`` grant
  when no signed ``MODEL.LICENSE`` is present. ``buffalo_l`` (same terms,
  ResNet50 recogniser, ~10x larger) can be selected with ``model_pack``.
  Embeddings from different packs are not comparable, so the saved index
  records its backend and refuses to load into another one.

insightface, onnxruntime, cv2 and numpy are imported only on the real-model
path, so an injected ``embedder`` needs none of them and tests load no model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_THRESHOLD = 0.45  # cosine similarity; below this the face is "unknown"
DEFAULT_MODEL_PACK = "buffalo_sc"
DEFAULT_MODEL_ROOT = ".cache/insightface"
DEFAULT_FACES_DIR = ".data/faces"
INDEX_FILENAME = "index.json"
MIN_FACE_PX = 20  # faces narrower than this give unreliable embeddings
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")
META_KEY = "_meta"
REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_ZOO_URL = "https://github.com/deepinsight/insightface/releases/download/model-zoo/"
# SHA-256 of the pack files the recorded measurements were made with.
MODEL_PACK_SHA256: dict[str, dict[str, str]] = {
    "buffalo_sc": {  # buffalo_sc.zip fetched 2026-09-19
        "det_500m.onnx": "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a",
        "w600k_mbf.onnx": "9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
    },
}

Embedder = Callable[[bytes, "Sequence[float] | None"], "Sequence[float] | None"]

FACES_README = """# Enrolled faces (local only, git-ignored)

Put a few clear, front-facing photos of each person in `<name>/*.jpg`, then run
`python -m robot.simulation.face_id enroll <name>` from the repository root.

Consent rules:

- Enrol only people who have explicitly agreed to be recognised by the robot.
- Never enrol the resident by default. Do it only with their own informed
  agreement (or that of whoever may lawfully agree for them), recorded by the team.
- Anyone can withdraw: delete their folder and run
  `python -m robot.simulation.face_id forget <name>`.
- Photos and `index.json` (face templates) are biometric data. Keep them on
  this machine; never commit, upload, or paste them into logs or chats.

A match is a similarity score against these photos, not proof of identity.
"""


class FaceIdError(RuntimeError):
    """Raised for a missing backend or model pack, unreadable images, or a bad index."""


def _repo_path(path: str | os.PathLike) -> Path:
    path = Path(path).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path  # repo-relative, not cwd-relative


def _valid_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = name.strip()
    # Names double as folder names and share the JSON object with META_KEY.
    if not name or len(name) > 64 or name[0] in "._" or any(c in name for c in '/\\:\0'):
        raise ValueError(f"invalid enrolment name: {name!r}")
    return name


def _unit(vector: Sequence[float]) -> list[float]:
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if not values or not math.isfinite(norm) or norm <= 0.0:
        raise FaceIdError("embedder returned an empty, zero or non-finite embedding")
    return [round(v / norm, 6) for v in values]


class InsightFaceEmbedder:
    """Real-model path: largest usable face in a region -> ArcFace embedding."""

    def __init__(
        self,
        *,
        model_pack: str = DEFAULT_MODEL_PACK,
        model_root: str = DEFAULT_MODEL_ROOT,
        det_size: int = 640,
        allow_download: bool = False,
    ):
        self.model_pack = str(model_pack)
        self.model_root = str(model_root)
        self.det_size = int(det_size)
        self.allow_download = bool(allow_download)
        self.backend = f"insightface/{self.model_pack}"
        self._app = None

    def pack_dir(self) -> Path:
        return _repo_path(self.model_root) / "models" / self.model_pack

    def verify_pack(self) -> dict[str, str]:
        """SHA-256 of each pack file; raises when a pinned hash does not match."""
        digests = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.pack_dir().glob("*.onnx"))
        }
        for filename, expected in MODEL_PACK_SHA256.get(self.model_pack, {}).items():
            if digests.get(filename) != expected:
                raise FaceIdError(f"{self.model_pack}/{filename} does not match its recorded SHA-256")
        return digests

    def load(self):
        if self._app is not None:
            return self._app
        try:
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise FaceIdError(
                "insightface not installed; see robot/simulation/requirements-perception.txt"
            ) from exc
        cached = any(self.pack_dir().glob("*.onnx"))
        if not cached and not self.allow_download:
            raise FaceIdError(
                f"model pack not found: {self.pack_dir()}; "
                "run `python -m robot.simulation.face_id setup` once while online"
            )
        try:
            if cached:
                self.verify_pack()  # before onnxruntime opens the files
            app = FaceAnalysis(  # fetches the pack when it is not cached
                name=self.model_pack,
                root=str(_repo_path(self.model_root)),
                allowed_modules=["detection", "recognition"],
                providers=["CPUExecutionProvider"],
            )
            if not cached:
                self.verify_pack()
            app.prepare(ctx_id=-1, det_thresh=0.5, det_size=(self.det_size, self.det_size))
        except FaceIdError:
            raise
        except Exception as exc:  # noqa: BLE001 - download or bad model file
            raise FaceIdError(f"cannot load model pack {self.model_pack}: {exc}") from exc
        self._app = app
        return app

    def __call__(self, jpeg_bytes: bytes, box_xyxy: Sequence[float] | None = None):
        import cv2
        import numpy as np

        app = self.load()
        # cv2 decodes to BGR, which is what InsightFace expects for ndarrays.
        image = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise FaceIdError("unreadable image: not a decodable JPEG")
        if box_xyxy is not None:
            height, width = image.shape[:2]
            x1, y1, x2, y2 = (float(v) for v in box_xyxy)
            x1, x2 = max(0, int(x1)), min(width, int(math.ceil(x2)))
            y1, y2 = max(0, int(y1)), min(height, int(math.ceil(y2)))
            if x2 - x1 < MIN_FACE_PX or y2 - y1 < MIN_FACE_PX:
                return None
            image = image[y1:y2, x1:x2]
        try:
            faces = app.get(image)
        except Exception as exc:  # noqa: BLE001 - inference failure is reportable
            raise FaceIdError(f"face inference failed: {exc}") from exc
        usable = [
            face
            for face in faces
            if face.embedding is not None
            and min(face.bbox[2] - face.bbox[0], face.bbox[3] - face.bbox[1]) >= MIN_FACE_PX
        ]
        if not usable:
            return None
        # The box owner is normally the largest face inside their own box.
        largest = max(usable, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return largest.normed_embedding.tolist()


class FaceIndex:
    """Enrolled name -> unit face embeddings, matched by cosine similarity."""

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        embedder: Embedder | None = None,
        model_pack: str = DEFAULT_MODEL_PACK,
        model_root: str = DEFAULT_MODEL_ROOT,
    ):
        if not -1.0 < threshold < 1.0:
            raise ValueError("threshold must be a cosine similarity in (-1, 1)")
        self.threshold = float(threshold)
        self._embedder = (
            embedder
            if embedder is not None
            else InsightFaceEmbedder(model_pack=model_pack, model_root=model_root)
        )
        self.backend = str(getattr(self._embedder, "backend", "custom"))
        self._people: dict[str, list[list[float]]] = {}

    def names(self) -> list[str]:
        return sorted(self._people)

    def _dim(self) -> int | None:
        for embeddings in self._people.values():
            return len(embeddings[0])
        return None

    def enroll(self, name: str, images: list[bytes]) -> int:
        """Add one embedding per image that shows a usable face; returns how many.

        Images without a usable face are skipped, and an image already
        enrolled for this name adds nothing, so re-running is harmless.
        """
        name = _valid_name(name)
        added = 0
        for jpeg_bytes in images:
            raw = self._embedder(jpeg_bytes, None)
            if raw is None:
                continue
            embedding = _unit(raw)
            if self._dim() not in (None, len(embedding)):
                raise FaceIdError("embedding size differs from the enrolled embeddings")
            known = self._people.setdefault(name, [])
            if embedding not in known:
                known.append(embedding)
                added += 1
        if not self._people.get(name):
            self._people.pop(name, None)
        return added

    def forget(self, name: str) -> int:
        """Remove a person's embeddings (consent withdrawn); returns how many."""
        return len(self._people.pop(name.strip(), []))

    def identify(self, jpeg_bytes: bytes, box_xyxy: Sequence[float] | None = None) -> dict | None:
        """Best enrolled match for the largest face inside ``box_xyxy``.

        ``box_xyxy`` is in original-image pixels (``None`` = whole image).
        Returns ``{'name', 'score'}`` with the raw cosine similarity, or
        ``None`` when nobody is enrolled, no usable face is visible, or the
        best score is below ``threshold``.
        """
        if not self._people:
            return None
        raw = self._embedder(jpeg_bytes, box_xyxy)
        if raw is None:
            return None
        query = _unit(raw)
        if len(query) != self._dim():
            raise FaceIdError("embedding size differs from the enrolled embeddings")
        best_name, best_score = None, -1.0
        for name in self.names():
            for embedding in self._people[name]:
                score = sum(a * b for a, b in zip(query, embedding))
                if score > best_score:
                    best_name, best_score = name, score
        score = round(max(-1.0, min(1.0, best_score)), 4)
        if best_name is None or score < self.threshold:
            return None
        return {"name": best_name, "score": score}

    def save(self, path: str | os.PathLike) -> None:
        """Write ``name -> list of embedding lists`` JSON, readable by the owner only."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {META_KEY: {"backend": self.backend, "dim": self._dim()}}
        payload.update({name: self._people[name] for name in self.names()})
        temporary = path.with_name(path.name + ".tmp")
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)

    def load(self, path: str | os.PathLike) -> "FaceIndex":
        """Replace this index with the saved one; returns ``self`` for chaining."""
        path = Path(path)
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise FaceIdError(f"face index not found: {path}") from exc
        except (OSError, ValueError) as exc:
            raise FaceIdError(f"invalid face index {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise FaceIdError(f"invalid face index {path}: expected a JSON object")
        meta = payload.pop(META_KEY, None) or {}
        saved_backend = meta.get("backend") if isinstance(meta, dict) else None
        if saved_backend and saved_backend != self.backend:
            raise FaceIdError(
                f"face index was built with {saved_backend}, not {self.backend}; "
                "embeddings from different models are not comparable - enrol again"
            )
        people: dict[str, list[list[float]]] = {}
        try:
            for name, embeddings in payload.items():
                if not isinstance(embeddings, list) or not all(isinstance(e, list) for e in embeddings):
                    raise ValueError(f"{name!r} is not a list of embedding lists")
                if embeddings:
                    people[_valid_name(name)] = [_unit(e) for e in embeddings]
        except (TypeError, ValueError, FaceIdError) as exc:
            raise FaceIdError(f"invalid face index {path}: {exc}") from exc
        if len({len(e) for embeddings in people.values() for e in embeddings}) > 1:
            raise FaceIdError(f"invalid face index {path}: mixed embedding sizes")
        self._people = people
        return self


def ensure_faces_dir(faces_dir: str | os.PathLike = DEFAULT_FACES_DIR) -> Path:
    """Create the ignored enrolment folder and its consent README if missing."""
    root = _repo_path(faces_dir)
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(FACES_README)
    return root


def photo_paths(folder: str | os.PathLike) -> list[Path]:
    return sorted(
        path
        for path in Path(folder).iterdir()
        if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES
    )


def enroll_directory(index: FaceIndex, faces_dir: str | os.PathLike = DEFAULT_FACES_DIR) -> dict[str, int]:
    """Enrol every ``<faces_dir>/<name>/*.jpg`` folder; returns name -> embeddings added."""
    root = _repo_path(faces_dir)
    if not root.is_dir():
        raise FaceIdError(f"faces directory not found: {root}")
    return {
        folder.name: index.enroll(folder.name, [p.read_bytes() for p in photo_paths(folder)])
        for folder in sorted(root.iterdir())
        if folder.is_dir() and folder.name[0] not in "._"
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m robot.simulation.face_id",
        description="Local face similarity matching. Enrol only people who consented.",
    )
    parser.add_argument("--faces-dir", default=DEFAULT_FACES_DIR)
    parser.add_argument("--index", default=None, help="index JSON (default <faces-dir>/index.json)")
    parser.add_argument("--model-pack", default=DEFAULT_MODEL_PACK)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="download and verify the model pack")
    enroll = commands.add_parser("enroll", help="enrol a consenting person")
    enroll.add_argument("name")
    enroll.add_argument("photos", nargs="*", help="default: <faces-dir>/<name>/*.jpg")
    forget = commands.add_parser("forget", help="remove a person's face templates")
    forget.add_argument("name")
    commands.add_parser("list", help="enrolled names")
    who = commands.add_parser("who", help="best enrolled match for the largest face in a photo")
    who.add_argument("photo")
    args = parser.parse_args(argv)

    faces_dir = ensure_faces_dir(args.faces_dir)
    index_path = Path(args.index) if args.index else faces_dir / INDEX_FILENAME
    # Operator-run commands may fetch the pack; the robot runtime never does.
    embedder = InsightFaceEmbedder(model_pack=args.model_pack, allow_download=True)
    index = FaceIndex(threshold=args.threshold, embedder=embedder)
    try:
        if args.command == "setup":
            embedder.load()
            result = {"model_pack": embedder.backend, "path": str(embedder.pack_dir()), "sha256": embedder.verify_pack()}
        else:
            if index_path.exists():
                index.load(index_path)
            if args.command == "enroll":
                name = _valid_name(args.name)
                photos = [Path(p) for p in args.photos] or photo_paths(faces_dir / name)
                added = index.enroll(name, [p.read_bytes() for p in photos])
                index.save(index_path)
                result = {"name": name, "photos": len(photos), "embeddings_added": added, "enrolled": index.names()}
            elif args.command == "forget":
                result = {"name": args.name, "embeddings_removed": index.forget(args.name)}
                index.save(index_path)
            elif args.command == "list":
                result = {"enrolled": index.names()}
            else:
                result = {"match": index.identify(Path(args.photo).read_bytes(), None), "threshold": index.threshold}
    except (FaceIdError, OSError, ValueError) as exc:
        print(f"face_id: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
