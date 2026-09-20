"""People Annie knows: enrol a person from photos (family app), list them, forget them; the running tracker
picks the updated face index up without a restart.

Photos are used once for the embedding and never stored; only `.data/faces/index.json` (embeddings, 0600)
persists. `PeopleDirectory.identify_shirt()` keeps the demo's shirt-colour identity next to the face index so
"Jeanine = red shirt" and "Jeanine = this face" both greet by name.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,39}$")


class PeopleDirectory:
    def __init__(self, faces_dir=".data/faces", *, index_factory=None, tracker=None):
        self.faces_dir = Path(faces_dir)
        self.index_path = self.faces_dir / "index.json"
        self.meta_path = self.faces_dir / "people.json"  # names, relation, notes (no images)
        self._index_factory = index_factory
        self._index = None
        self.tracker = tracker
        self.lock = threading.Lock()
        self.meta = self._load_meta()

    # -- storage ----------------------------------------------------------------------------------------------
    def _load_meta(self) -> dict:
        try:
            data = json.loads(self.meta_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_meta(self):
        self.faces_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_name(self.meta_path.name + ".tmp")
        tmp.write_text(json.dumps(self.meta, indent=1))
        tmp.replace(self.meta_path)

    def index(self):
        """The face index (InsightFace by default), loaded from disk when present; None when the model stack
        is unavailable (then people are known by name/shirt only)."""
        if self._index is None:
            try:
                if self._index_factory is not None:
                    self._index = self._index_factory()
                else:
                    from robot.simulation.face_id import FaceIndex
                    self._index = FaceIndex()
                if self.index_path.exists():
                    self._index.load(self.index_path)
            except Exception:
                self._index = None
        return self._index

    # -- API --------------------------------------------------------------------------------------------------
    def list(self) -> list[dict]:
        idx = self.index()
        names = set(self.meta) | (set(idx.names()) if idx is not None else set())
        out = []
        for name in sorted(names):
            m = self.meta.get(name, {})
            out.append({"name": name, "relation": m.get("relation"), "shirt": m.get("shirt"), "notes": m.get("notes"),
                        "faces": len(getattr(idx, "_people", {}).get(name, [])) if idx is not None else 0,
                        "added_at": m.get("added_at")})
        return out

    def enroll(self, name: str, images: list[bytes], *, relation=None, shirt=None, notes=None) -> dict:
        """Add a person: metadata always, face embeddings when the images show a usable face."""
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("name must be 1-40 letters, spaces, apostrophes or hyphens")
        added, faces_error = 0, None
        with self.lock:
            idx = self.index()
            if idx is not None and images:
                try:
                    added = idx.enroll(name, [img for img in images if isinstance(img, (bytes, bytearray))][:10])
                    if added:
                        idx.save(self.index_path)
                except Exception as exc:
                    faces_error = type(exc).__name__
            elif images and idx is None:
                faces_error = "face model unavailable"
            m = self.meta.setdefault(name, {"added_at": time.time()})
            if relation is not None:
                m["relation"] = str(relation)[:40] or None
            if shirt is not None:
                m["shirt"] = str(shirt)[:20].lower() or None
            if notes is not None:
                m["notes"] = str(notes)[:200] or None
            self._save_meta()
            self._push_to_tracker()
        return {"name": name, "faces_added": added, "faces_total": len(getattr(idx, "_people", {}).get(name, [])) if idx is not None else 0,
                "faces_error": faces_error, "relation": self.meta[name].get("relation"), "shirt": self.meta[name].get("shirt")}

    def forget(self, name: str) -> bool:
        with self.lock:
            idx = self.index()
            removed = False
            if idx is not None and name in idx.names():
                idx.forget(name)
                idx.save(self.index_path)
                removed = True
            if name in self.meta:
                del self.meta[name]
                self._save_meta()
                removed = True
            self._push_to_tracker()
            return removed

    def _push_to_tracker(self):
        """The live tracker identifies with the same index object; a fresh index after a change is enough."""
        if self.tracker is not None and self._index is not None:
            try:
                self.tracker.face_index = self._index
            except Exception:
                pass

    def shirt_identities(self) -> list[tuple[str, str]]:
        """[(name, colour)] for the shirt-colour identifier (demo: Jeanine = red)."""
        return [(n, m["shirt"]) for n, m in self.meta.items() if m.get("shirt")]
