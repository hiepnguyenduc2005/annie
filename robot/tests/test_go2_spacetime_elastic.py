"""Space-time -> Elasticsearch documents and queries, with a fake HTTP client (no node needed)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_spacetime_elastic import SpacetimeIndex, documents_from_line, tail_file  # noqa: E402


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class FakeES:
    def __init__(self):
        self.docs, self.requests = {}, []

    def get(self, path):
        return FakeResp(200, {"status": "green"})

    def head(self, path):
        return FakeResp(404)

    def put(self, path, json=None):
        self.requests.append(("put", path))
        return FakeResp(200)

    def post(self, path, content=None, json=None, headers=None, params=None):
        self.requests.append(("post", path))
        if path == "/_bulk":
            lines = content.strip().split("\n")
            items = []
            for meta, doc in zip(lines[::2], lines[1::2]):
                self.docs[__import__("json").loads(meta)["index"]["_id"]] = __import__("json").loads(doc)
                items.append({"index": {"status": 201}})
            return FakeResp(200, {"items": items})
        if path.endswith("/_search"):
            q = json
            docs = list(self.docs.values())
            filters = q["query"].get("bool", {}).get("filter", [])
            for f in filters:
                if "term" in f:
                    (k, v), = f["term"].items()
                    docs = [d for d in docs if d.get(k) == v]
            if "match" in q["query"]:
                words = q["query"]["match"]["text"]["query"].lower().split()
                docs = [d for d in docs if any(w in (d.get("text") or "").lower() for w in words)]
            docs.sort(key=lambda d: d["@timestamp"], reverse=True)
            return FakeResp(200, {"hits": {"hits": [{"_source": d} for d in docs[: q.get("size", 10)]]}})
        return FakeResp(404)


def test_documents_from_people_and_events_only():
    people = {"type": "people", "wall_ms": 1789880000000, "people": [
        {"track_id": 3, "x": 1.0, "y": 2.0, "z": 0.9, "label": "Jeanine", "identity": "Jeanine", "posture": "lying"}]}
    docs = documents_from_line(people, "run-1")
    assert docs[0]["identity"] == "Jeanine" and docs[0]["text"] == "Jeanine seen lying down" and docs[0]["kind"] == "person"
    ev = documents_from_line({"type": "event", "wall_ms": 1789880001000, "x": 0.5, "y": 0.5, "kind": "greet", "text": "Jeanine"}, "run-1")
    assert ev[0]["kind"] == "greet" and ev[0]["text"] == "Jeanine"
    assert documents_from_line({"type": "obstacles", "t": 1.0, "points": []}, "run-1") == []


def test_tail_indexes_and_last_seen_answers_with_time_and_position(tmp_path):
    path = tmp_path / "spacetime.jsonl"
    lines = [
        {"type": "people", "wall_ms": 1789880000000, "people": [{"track_id": 1, "x": 3.0, "y": 0.2, "z": 0.9, "label": "Jeanine", "identity": "Jeanine", "posture": "upright"}]},
        {"type": "obstacles", "wall_ms": 1789880000500, "points": [[1, 2, 0.3]]},
        {"type": "people", "wall_ms": 1789880060000, "people": [{"track_id": 9, "x": -1.0, "y": 2.5, "z": 0.9, "label": "Jeanine", "identity": "Jeanine", "posture": "lying"}]},
        {"type": "event", "wall_ms": 1789880061000, "x": -0.8, "y": 2.2, "kind": "checkin", "text": "asked if alright"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    fake = FakeES()
    index = SpacetimeIndex(url="http://127.0.0.1:9200", api_key="")
    index._client = fake
    assert index.ensure_index() and ("put", "/annie-spacetime") in fake.requests
    assert tail_file(path, index, "run-7") == 3  # two sightings + one event; obstacles skipped
    seen = index.last_seen("Jeanine")
    assert seen["x"] == -1.0 and seen["posture"] == "lying" and seen["when"].startswith("2026-09-20")
    hits = index.search("lying")
    assert hits and hits[0]["identity"] == "Jeanine"
    # replaying the same file is idempotent (same document ids)
    tail_file(path, index, "run-7")
    assert len(fake.docs) == 3
