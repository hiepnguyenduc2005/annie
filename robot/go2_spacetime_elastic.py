"""Elasticsearch over the dog's space-time stream: sightings and events as timestamped, positioned documents.

The recorder (`go2_spacetime.py`) writes what the dog saw as JSON lines: poses, LiDAR
obstacle frames, people estimates and events. This module turns the *people and event*
lines (never raw frames) into documents in one index, so the family-facing memory can
answer "where was Jeanine last seen?" or "what happened near the kitchen at 9pm?" with
a timestamp and a position. It runs as a tailer (`--tail`) beside the patrol, or
indexes a finished recording once. Positions are odometry-frame estimates; a document
is evidence of an observation, not proof of where someone is now.

Index `annie-spacetime` (created on first use, strict mapping):
  @timestamp, kind (person|greet|checkin|collision|voice|brain|stuck|seen),
  x, y, z (metres, odom frame), label, identity, posture, text, track_id, run_id,
  location (geo_point-like [x, y] kept as two floats for range queries).

Demo profile: a loopback single-node Elasticsearch without TLS
(`ELASTIC_URL=http://127.0.0.1:9200`, no key). Production: HTTPS + API key, see
`docs/LOCAL_ENV.md`. Nothing here leaves the machine unless ELASTIC_URL points elsewhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_INDEX = "annie-spacetime"
MAPPING = {"mappings": {"dynamic": "strict", "properties": {
    "@timestamp": {"type": "date"}, "kind": {"type": "keyword"}, "run_id": {"type": "keyword"},
    "x": {"type": "float"}, "y": {"type": "float"}, "z": {"type": "float"},
    "label": {"type": "keyword"}, "identity": {"type": "keyword"}, "posture": {"type": "keyword"},
    "track_id": {"type": "integer"}, "text": {"type": "text"}}}}


def _iso(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def documents_from_line(line: dict, run_id: str) -> list[dict]:
    """JSONL record -> documents (people and events only; poses/obstacles are not indexed)."""
    kind = line.get("type")
    ts = line.get("wall_ms") or int(float(line.get("t", 0)) * 1000)
    if kind == "people":
        return [{"@timestamp": _iso(ts), "kind": "person", "run_id": run_id, "x": float(p.get("x", 0)), "y": float(p.get("y", 0)),
                 "z": float(p.get("z", 0)), "label": str(p.get("label") or ""), "identity": p.get("identity"),
                 "posture": p.get("posture"), "track_id": p.get("track_id"),
                 "text": f"{p.get('identity') or p.get('label') or 'person'} seen{' lying down' if p.get('posture') == 'lying' else ''}"}
                for p in line.get("people", []) if isinstance(p, dict)]
    if kind == "event":
        return [{"@timestamp": _iso(ts), "kind": str(line.get("kind") or "event"), "run_id": run_id,
                 "x": float(line.get("x", 0)), "y": float(line.get("y", 0)), "z": 0.0, "label": str(line.get("kind") or ""),
                 "identity": None, "posture": None, "track_id": None, "text": str(line.get("text") or line.get("kind") or "")}]
    return []


class SpacetimeIndex:
    """Small synchronous client (httpx); loopback HTTP without a key is accepted for the demo profile."""

    def __init__(self, url=None, index=DEFAULT_INDEX, api_key=None, ca_cert=None, timeout_s=5.0):
        self.url = (url or os.environ.get("ELASTIC_URL") or "http://127.0.0.1:9200").rstrip("/")
        self.index = index
        self.api_key = api_key if api_key is not None else os.environ.get("ELASTIC_API_KEY") or None
        self.ca_cert = ca_cert if ca_cert is not None else os.environ.get("ELASTIC_CA_CERT") or None
        self.timeout_s = timeout_s
        self._client = None

    def client(self):
        import httpx
        if self._client is None:
            headers = {"Authorization": f"ApiKey {self.api_key}"} if self.api_key else {}
            self._client = httpx.Client(base_url=self.url, headers=headers, timeout=self.timeout_s, trust_env=False,
                                        verify=self.ca_cert or True)
        return self._client

    def ready(self) -> bool:
        try:
            return self.client().get("/_cluster/health").status_code == 200
        except Exception:
            return False

    def ensure_index(self) -> bool:
        c = self.client()
        if c.head(f"/{self.index}").status_code == 200:
            return True
        return c.put(f"/{self.index}", json=MAPPING).status_code in (200, 201)

    def bulk(self, docs: list[dict]) -> int:
        if not docs:
            return 0
        lines = []
        for d in docs:
            key = f"{d['@timestamp']}|{d['kind']}|{d.get('track_id')}|{d['x']:.2f}|{d['y']:.2f}"
            lines.append(json.dumps({"index": {"_index": self.index, "_id": key}}))  # idempotent on replay
            lines.append(json.dumps(d))
        r = self.client().post("/_bulk", params={"refresh": "wait_for"}, content="\n".join(lines) + "\n",
                               headers={"Content-Type": "application/x-ndjson"})
        r.raise_for_status()
        body = r.json()
        return sum(1 for item in body.get("items", []) if item.get("index", {}).get("status") in (200, 201))

    def last_seen(self, name: str, *, within_hours=48.0) -> dict | None:
        """Most recent sighting of an identified person: {"when", "x", "y", "posture", "age_s"} or None."""
        q = {"size": 1, "sort": [{"@timestamp": "desc"}],
             "query": {"bool": {"filter": [{"term": {"identity": name}}, {"term": {"kind": "person"}},
                                            {"range": {"@timestamp": {"gte": f"now-{int(within_hours)}h"}}}]}}}
        hits = self.client().post(f"/{self.index}/_search", json=q).json().get("hits", {}).get("hits", [])
        if not hits:
            return None
        src = hits[0]["_source"]
        when = datetime.fromisoformat(src["@timestamp"])
        return {"when": src["@timestamp"], "x": src["x"], "y": src["y"], "posture": src.get("posture"),
                "age_s": round((datetime.now(timezone.utc) - when).total_seconds(), 1)}

    def search(self, text: str, *, size=5) -> list[dict]:
        """Free-text over what happened, newest first, each hit with its time and position."""
        q = {"size": size, "sort": [{"@timestamp": "desc"}], "query": {"match": {"text": {"query": text, "operator": "or"}}}}
        hits = self.client().post(f"/{self.index}/_search", json=q).json().get("hits", {}).get("hits", [])
        return [{k: h["_source"].get(k) for k in ("@timestamp", "kind", "x", "y", "identity", "posture", "text")} for h in hits]


def tail_file(path: Path, index: SpacetimeIndex, run_id: str, *, follow=False, poll_s=1.0, status=print) -> int:
    """Index every people/event line in the JSONL, optionally following as it grows. Returns docs indexed."""
    total, offset = 0, 0
    batch: list[dict] = []
    while True:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                fh.seek(offset)
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    if not raw.endswith("\n"):
                        break  # a partial line still being written: re-read it next pass
                    offset = fh.tell()
                    try:
                        batch.extend(documents_from_line(json.loads(raw), run_id))
                    except (ValueError, TypeError):
                        continue
                    if len(batch) >= 200:
                        total += index.bulk(batch)
                        batch = []
        if batch:
            total += index.bulk(batch)
            batch = []
            status(f"spacetime-elastic: {total} documents indexed")
        if not follow:
            return total
        time.sleep(poll_s)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index the dog's space-time stream into Elasticsearch.")
    parser.add_argument("--file", default=".data/hardware/spacetime.jsonl")
    parser.add_argument("--tail", action="store_true", help="keep following the file")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M"))
    parser.add_argument("--last-seen", metavar="NAME", help="query instead of indexing")
    parser.add_argument("--search", metavar="TEXT")
    args = parser.parse_args(argv)
    index = SpacetimeIndex()
    if not index.ready():
        print(f"spacetime-elastic: Elasticsearch not reachable at {index.url}", file=sys.stderr)
        return 2
    index.ensure_index()
    if args.last_seen:
        print(json.dumps(index.last_seen(args.last_seen)))
        return 0
    if args.search:
        print(json.dumps(index.search(args.search), indent=1))
        return 0
    n = tail_file(Path(args.file), index, args.run_id, follow=args.tail, status=lambda t: print(t, file=sys.stderr, flush=True))
    print(json.dumps({"indexed": n, "index": index.index, "url": index.url}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
