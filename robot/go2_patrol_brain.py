"""Brain layer for the physical patrol: a local VLM proposes the next move, deterministic guardrails dispose.

Every few seconds (never while the dog is following or greeting someone, which stay
reactive), `VisionBrain.decide` sends the annotated camera frame plus a compact status
line and the sighting memory to a local vision-language model and gets back one action
from a fixed menu with a one-line reason. The patrol runtime turns that into velocities,
but the collision, stall, battery, leash and stale-link guardrails in
`go2_smart_patrol` / `go2_patrol_greet` still override it at every tick, and a
visible person always takes priority over whatever the brain wanted.

`SightingMemory` is the dog's short spatial memory: where people were seen and where
it already walked up and said hello (pose + heading + time). It feeds the prompt
("greeted someone 2 m ahead-left 40 s ago") and a deterministic rule that stops the
dog re-approaching the same spot within a cooldown, since the tracker hands out a
new id every time a person leaves and re-enters the frame. Entries are appended to a
JSONL file so a later run, or the knowledge graph, can pick them up.

The model is a proposal engine, not a safety system, and its reasons are text, not
evidence. Model failures or timeouts fall back to the deterministic planner.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import time
from pathlib import Path

ACTIONS = ("explore", "turn_left", "turn_right", "scan", "approach", "go_home", "wait")
DEFAULT_MODEL = os.environ.get("ANNIE_PATROL_BRAIN_MODEL", "qwen3-vl:2b-instruct")
DEFAULT_BASE_URL = os.environ.get("ANNIE_PATROL_BRAIN_URL", "http://127.0.0.1:11434/v1")
MAX_SECONDS = 8.0

SYSTEM = (
    "You steer a small quadruped robot dog patrolling an indoor event space to greet people. "
    "You see its camera (green boxes = tracked people, grey boxes = unconfirmed detections) and a status line. "
    "Pick ONE action for the next few seconds from: explore (walk ahead, curving gently), turn_left, turn_right, "
    "scan (slow full turn to look around), approach (walk toward the people you see), go_home (return to start), "
    "wait (stand still). Prefer finding and approaching people you have NOT greeted recently; avoid walking into "
    "furniture; do not re-approach a spot where you greeted someone moments ago. "
    'Answer with JSON only: {"action": "...", "seconds": 2-8, "reason": "<=12 words"}.'
)


def bearing_deg(pose_xy, yaw, target_xy) -> float:
    """Bearing of a world point relative to the robot heading, degrees, +left."""
    dx, dy = target_xy[0] - pose_xy[0], target_xy[1] - pose_xy[1]
    return math.degrees((math.atan2(dy, dx) - yaw + math.pi) % (2 * math.pi) - math.pi)


class SightingMemory:
    """Where people were seen and greeted; small, time-decayed, persisted as JSONL."""

    def __init__(self, path: str | os.PathLike | None = None, *, keep_s=600.0):
        self.path = Path(path) if path else None
        self.keep_s = keep_s
        self.entries: list[dict] = []

    def add(self, *, t, pose, yaw, kind, people=0, note=None):
        entry = {"t": float(t), "x": float(pose[0]), "y": float(pose[1]), "yaw": float(yaw), "kind": kind,
                 "people": int(people), "note": note}
        self.entries.append(entry)
        self.entries = [e for e in self.entries if t - e["t"] <= self.keep_s][-200:]
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({**entry, "wall_ms": int(time.time() * 1000)}) + "\n")
            except OSError:
                pass
        return entry

    def greeted_here_recently(self, pose, yaw, *, now, radius_m=1.2, within_s=90.0, heading_deg=40.0) -> bool:
        """True when the dog already greeted someone from about this spot and heading a moment ago."""
        for e in self.entries:
            if e["kind"] != "greet" or now - e["t"] > within_s:
                continue
            if math.dist(pose, (e["x"], e["y"])) > radius_m:
                continue
            if abs(math.degrees((yaw - e["yaw"] + math.pi) % (2 * math.pi) - math.pi)) <= heading_deg:
                return True
        return False

    def summary(self, pose, yaw, *, now, limit=5) -> list[str]:
        out = []
        for e in reversed(self.entries[-40:]):
            age = now - e["t"]
            dist = math.dist(pose, (e["x"], e["y"]))
            here = dist < 0.5
            where = "here" if here else f"{dist:.1f} m away, bearing {bearing_deg(pose, yaw, (e['x'], e['y'])):+.0f} deg"
            label = {"greet": "greeted someone", "seen": f"saw {e['people']} person(s)", "checkin": "checked on someone lying down",
                     "collision": "bumped into something", "blocked": "obstacle ahead"}.get(e["kind"], e["kind"])
            out.append(f"{label} {where}, {age:.0f} s ago")
            if len(out) >= limit:
                break
        return out


def build_status_line(status: dict) -> str:
    r = status.get("ranges") or {}

    def fmt(v):
        return "clear" if v is None or v == float("inf") else f"{v:.1f} m"
    return (f"people_in_view={status.get('people', 0)} lidar front={fmt(r.get('front'))} left={fmt(r.get('left'))} "
            f"right={fmt(r.get('right'))} distance_from_start={status.get('home_m', 0.0):.1f} m "
            f"(leash {status.get('leash_m', 2.0):.1f} m) battery={status.get('battery', 0) or 0:.0f}% "
            f"last_mode={status.get('mode', '-')} greeted_total={status.get('greetings', 0)}")


def build_user_prompt(status: dict, sightings: list[str]) -> str:
    mem = "\n".join(f"- {s}" for s in sightings) or "- nothing yet"
    return f"Status: {build_status_line(status)}\nMemory:\n{mem}\nWhat should the dog do next? JSON only."


def parse_decision(text: str) -> dict:
    """Extract {"action","seconds","reason"}; anything unusable becomes a bounded explore."""
    fallback = {"action": "explore", "seconds": 3.0, "reason": "unparseable brain reply", "ok": False}
    if not isinstance(text, str) or not text.strip():
        return fallback
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return fallback
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return fallback
    action = str(data.get("action", "")).strip().lower()
    if action not in ACTIONS:
        return fallback
    try:
        seconds = float(data.get("seconds", 3.0))
    except (TypeError, ValueError):
        seconds = 3.0
    seconds = min(MAX_SECONDS, max(1.0, seconds)) if math.isfinite(seconds) else 3.0
    reason = str(data.get("reason", ""))[:120]
    return {"action": action, "seconds": seconds, "reason": reason, "ok": True}


class VisionBrain:
    """OpenAI-compatible chat call with one image; loopback only, bounded, synchronous (run it in a thread)."""

    def __init__(self, *, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, timeout_s=10.0):
        from urllib.parse import urlsplit
        host = urlsplit(base_url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the patrol brain only talks to a loopback model server (resident frames stay local)")
        self.base_url, self.model, self.timeout_s = base_url.rstrip("/"), model, timeout_s
        self.calls, self.failures, self.last_ms = 0, 0, None

    def decide(self, jpeg: bytes | None, status: dict, sightings: list[str]) -> dict:
        import httpx
        content = [{"type": "text", "text": build_user_prompt(status, sightings)}]
        if jpeg:
            content.insert(0, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                                                                          + base64.b64encode(jpeg).decode()}})
        body = {"model": self.model, "messages": [{"role": "system", "content": SYSTEM},
                                                  {"role": "user", "content": content}],
                "max_tokens": 80, "temperature": 0.2, "stream": False}
        t0 = time.perf_counter()
        self.calls += 1
        try:
            with httpx.Client(timeout=self.timeout_s, trust_env=False) as client:
                resp = client.post(self.base_url + "/chat/completions", json=body)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            self.failures += 1
            self.last_ms = round((time.perf_counter() - t0) * 1000)
            return {"action": "explore", "seconds": 3.0, "reason": f"brain unavailable ({type(exc).__name__})", "ok": False}
        self.last_ms = round((time.perf_counter() - t0) * 1000)
        decision = parse_decision(text)
        decision["latency_ms"] = self.last_ms
        return decision
