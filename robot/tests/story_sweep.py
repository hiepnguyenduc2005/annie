"""Monte-Carlo sweep of the demo stories on virtual time (no hardware, no audio, no network).

    PY=.cache/dimos/.venv/bin/python
    $PY robot/tests/story_sweep.py --story both --seeds 10 --workers 2 --out /tmp/story_sweep.json

Runs story_harness.run_story_a/b over a bounded seed range, one process per run
(2 workers by default so the machine stays responsive), and writes one JSON file with
per-seed results plus a failure summary. Exit code 0 only when every run passed.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.tests import story_harness  # noqa: E402

STORIES = {"a": story_harness.run_story_a, "b": story_harness.run_story_b}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--story", choices=("a", "b", "both"), default="both")
    parser.add_argument("--seeds", type=int, default=10, help="seeds 1..N")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--out", default="/tmp/story_sweep_%s.json" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    args = parser.parse_args(argv)

    seeds = range(1, args.seeds + 1)
    names = ("a", "b") if args.story == "both" else (args.story,)
    t0 = time.time()
    results = {f"story_{name}": story_harness.run_many(STORIES[name], seeds, workers=args.workers) for name in names}
    wall_s = round(time.time() - t0, 1)

    failures = [dict(story=story, seed=r["seed"], reason=r["reason"], forced=r.get("forced"))
                for story, runs in results.items() for r in runs if not r["ok"]]
    payload = {
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(), "python": sys.version.split()[0],
        "passed": {story: sum(r["ok"] for r in runs) for story, runs in results.items()},
        "total": {story: len(runs) for story, runs in results.items()},
        "failures": failures, "wall_s": wall_s, "results": results,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, default=str))
    print(json.dumps({k: payload[k] for k in ("captured_at_utc", "passed", "total", "failures", "wall_s")}, indent=1))
    print("file", args.out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
