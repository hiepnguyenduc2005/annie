"""Annie as an MCP server: any MCP client (Claude Code, Claude Desktop, a dimOS agent) can drive the dog.

  .venv/bin/python robot/dog/mcp_server.py            # stdio; registered for this repo in .mcp.json
  claude mcp add annie -- .venv/bin/python robot/dog/mcp_server.py

A thin, stateless bridge. Every tool is one or two HTTP calls to the dog process (`robot/dog/runtime/patrol.py`,
default http://127.0.0.1:8111, THE SIMULATOR), which owns the robot, validates every command against the body
contract and keeps the guardrails (collision, stall, battery, leash, link). Nothing here talks to the robot
directly, so a tool can never do more than the command-center page can. Point ANNIE_BODY_URL elsewhere only
deliberately: port 8011 is the PHYSICAL dog.

  ANNIE_BODY_URL    base URL of the dog process (default http://127.0.0.1:8111, the simulator)
  ANNIE_BODY_TOKEN  sent as X-Body-Token when set (the dog process checks it on every POST)

THIS MOVES A REAL ROBOT. `stop` is a software stop through the patrol loop, not a hardware emergency stop.
Results are acknowledgments, transcripts and perception estimates: never proof that a motion happened, never a
health assessment. Tools return plain dicts with "ok"; failures carry a readable "error" instead of raising
(dog offline -> {"ok": false, "error": "the dog process is not reachable"}).

Self-contained on purpose (httpx + mcp only, no `robot.` imports): it is started as a script by MCP clients,
and the rest of the dog stack (dimOS, torch, the WebRTC driver) must not be needed to talk to a running dog.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
# Started as a script, Python puts robot/dog/ first on sys.path, where `dimos/`, `memory/`, `voice/` ... would
# shadow real top-level packages for every later import. This file needs nothing from there.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]

import anyio  # noqa: E402
import httpx  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ModuleNotFoundError:  # mcp 2.x renamed FastMCP to MCPServer; tool()/resource()/run() are unchanged
    from mcp.server.mcpserver import MCPServer as FastMCP

DEFAULT_BODY_URL = "http://127.0.0.1:8111"  # simulator; 8011 is the physical dog process
TIMEOUT_S = 5.0
TERMINAL = ("completed", "failed", "cancelled")
ACTIONS = ("explore", "scan", "go_home", "stop", "hello", "dance", "sit", "stand", "follow")
OFFLINE = "the dog process is not reachable"
STOP_NOTE = ("software stop through the patrol loop (cancels the mission, sends StopMove, and the dog holds still until "
             "you send another command); NOT a hardware emergency stop. Use the remote or lift the dog if it must stay stopped.")

INSTRUCTIONS = (
    "Annie is a Unitree Go2 robot dog that looks after an older adult at home. These tools drive the REAL robot through "
    "its dog process. Call dog_status first. Use instruct for anything in natural language ('check on Jeanine', 'remind "
    "Jeanine to drink some water', 'go to the door'); the dog plans it with its own guardrails. Use stop at once if anyone "
    "asks, or if something looks wrong: it is a software stop, not a hardware emergency stop. Results are acknowledgments, "
    "transcripts and perception estimates, not proof and not medical facts. Never diagnose; if someone may need help, say "
    "so plainly and tell the family.")


class DogClient:
    """Bounded HTTP calls to the dog process. Every method returns a dict with "ok"; none raises."""

    def __init__(self, base_url: str | None = None, token: str | None = None, *, transport=None, timeout_s: float = TIMEOUT_S,
                 poll_s: float = 0.5, sleep=anyio.sleep, clock=time.monotonic):
        self.base_url = (base_url or os.environ.get("ANNIE_BODY_URL") or DEFAULT_BODY_URL).rstrip("/")
        token = token if token is not None else os.environ.get("ANNIE_BODY_TOKEN")
        self.headers = {"X-Body-Token": token} if token else {}
        self.transport, self.timeout_s, self.poll_s, self.sleep, self.clock = transport, timeout_s, poll_s, sleep, clock

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=self.timeout_s, trust_env=False,
                                 transport=self.transport)

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """{"ok": True, "status", "data"} or {"ok": False, "error", ...}; `data` is the dog's JSON reply."""
        try:
            async with self._http() as http:
                r = await http.request(method, path, json=payload) if payload is not None else await http.request(method, path)
        except (httpx.TransportError, OSError):  # refused, timed out, DNS, reset: the client cannot fix any of them
            return {"ok": False, "error": OFFLINE, "url": self.base_url,
                    "hint": "start it with robot/demo_dog.sh, or point ANNIE_BODY_URL at the running dog process"}
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            reason = data.get("error") if isinstance(data, dict) else None
            return {"ok": False, "status": r.status_code, "error": _http_error(r.status_code, reason), "detail": data}
        if data is None:
            return {"ok": False, "status": r.status_code, "error": "the dog process sent a reply that is not JSON"}
        return {"ok": True, "status": r.status_code, "data": data}

    async def submit(self, name: str, args: dict | None = None, *, wait_s: float = 0.0) -> dict:
        """POST a body command; with `wait_s` poll its receipt until it is terminal or the wait runs out."""
        cid = str(uuid.uuid4())
        sent = await self.request("POST", "/command", {"command_id": cid, "name": name, "args": args or {}})
        if not sent["ok"]:
            if sent.get("error") == OFFLINE:  # the POST may or may not have arrived (read timeout): never resend,
                return {**sent, "command_id": cid, "state": "unknown",  # let the caller poll the id instead
                        "note": "the command may or may not have been accepted; poll command_status with this command_id instead of sending it again"}
            return sent
        receipt = sent["data"] if isinstance(sent["data"], dict) else {}
        deadline = self.clock() + max(0.0, wait_s)
        while wait_s > 0 and receipt.get("state") not in TERMINAL and self.clock() < deadline:
            await self.sleep(self.poll_s)
            polled = await self.request("GET", f"/command/{cid}")
            if not polled["ok"]:
                return {**polled, "command_id": cid, "note": "the command was accepted, but its receipt could not be read"}
            receipt = polled["data"] if isinstance(polled["data"], dict) else receipt
        return _receipt(receipt, cid)


def _http_error(status: int, reason) -> str:
    known = {401: "the dog process rejected the token: set ANNIE_BODY_TOKEN to the dog's body token",
             404: "the dog process does not know that (unknown id, or that part of it is not running)",
             409: "the dog is busy and its queue is full: wait, or call stop",
             421: "the dog process refused this Host: use its 127.0.0.1 address",
             503: f"that part of the dog process is off ({reason})" if reason else "that part of the dog process is off"}
    if status in known:
        return known[status]
    return f"the dog process refused it: {reason}" if reason else f"the dog process answered HTTP {status}"


def _receipt(receipt: dict, cid: str) -> dict:
    """A body-command receipt as a tool result. ok = accepted and not failed; `state` says how far it got."""
    state = receipt.get("state")
    out = {"ok": state not in ("failed", "cancelled"), "command_id": receipt.get("command_id") or cid, "name": receipt.get("name"),
           "state": state, "result": receipt.get("result"), "error": receipt.get("error")}
    if receipt.get("progress"):
        out["progress"] = receipt["progress"]
    if state == "queued":
        out["note"] = f"queued behind another command (position {receipt.get('position')}); poll command_status"
    elif state not in TERMINAL:
        out["note"] = "still running; poll command_status with this command_id, or call stop"
    return out


CLIENT: DogClient | None = None


def client() -> DogClient:
    """The shared client (tests replace `CLIENT`). Built on first use so the environment is read at call time."""
    global CLIENT
    if CLIENT is None:
        CLIENT = DogClient()
    return CLIENT


def _text(value, limit: int) -> str | None:
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


def _bounded_number(value, lo: float, hi: float, what: str) -> float:
    """A finite number clamped to [lo, hi]; raises ValueError for bools, non-numbers and nan/inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number ({lo:g} to {hi:g})")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number ({lo:g} to {hi:g})")
    return min(hi, max(lo, value))


def _object_data(result: dict, what: str) -> dict:
    """The dog's JSON reply when it is an object; otherwise result is turned into a failure result (check
    result["ok"] after calling) and that failure dict is returned instead."""
    data = result.get("data")
    if isinstance(data, dict):
        return data
    result["ok"], result["error"] = False, f"the dog process sent {what} that is not a JSON object"
    return result


logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per poll would flood the client's server log

mcp = FastMCP("annie", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------------------------------------------
# status and memory (read-only)
# ---------------------------------------------------------------------------------------------------------------
@mcp.tool()
async def dog_status() -> dict:
    """What Annie is doing right now: live state, recent greetings, missions, what she remembers, voice setup."""
    got = await client().request("GET", "/telemetry.json")
    if not got["ok"]:
        return got
    t = _object_data(got, "status")
    if not got["ok"]:
        return t
    return {"ok": True, "connected": t.get("connected"), "state": t.get("state"), "greetings": t.get("greetings") or [],
            "missions": t.get("missions") or [], "graph_sentences": t.get("graph_sentences") or [], "voice": t.get("voice") or {},
            "concerns": t.get("concerns") or [], "source": t.get("source") or "hardware", "conversations": t.get("conversations") or []}


@mcp.tool()
async def memory(limit: int = 6) -> dict:
    """What Annie remembers, as short sentences (who and what she saw, where, how long ago) plus recent events."""
    if isinstance(limit, bool):
        return {"ok": False, "error": "limit must be a whole number (1-30)"}
    if not (isinstance(limit, int) and math.isfinite(limit)):
        return {"ok": False, "error": "limit must be a finite whole number (1-30)"}
    limit = min(30, max(1, limit))
    tel = await client().request("GET", "/telemetry.json")
    if not tel["ok"]:
        return tel
    data = _object_data(tel, "memory")
    if not tel["ok"]:
        return data
    sentences = list(data.get("graph_sentences") or [])[:limit]
    out = {"ok": True, "sentences": sentences, "events": [],
           "caveat": "model guesses with odometry positions; the dog process keeps at most 6 sentences"}
    graph = await client().request("GET", "/graph.json")
    if graph["ok"]:
        gdata = _object_data(graph, "memory events")
        if not graph["ok"]:
            return {**out, "ok": False, "error": gdata["error"]}
        out["events"] = [{k: e.get(k) for k in ("t", "kind", "text", "place_name")} for e in (gdata.get("events") or [])[-limit:]
                         if isinstance(e, dict)]
    return out


@mcp.tool()
async def where_is(name: str) -> dict:
    """Where a person or object was last seen, from Annie's memory. An estimate with its age, not a live fix."""
    want = (_text(name, 80) or "").lower()
    if not want:
        return {"ok": False, "error": "say who or what to look up"}
    got = await client().request("GET", "/graph.json")
    if not got["ok"]:
        return got
    data = _object_data(got, "memory")
    if not got["ok"]:
        return data
    entities = [e for e in data.get("entities") or [] if isinstance(e, dict)]
    hits = [e for e in entities if want in " ".join(str(e.get(k) or "") for k in ("identity", "name", "label")).lower()]
    if not hits:
        return {"ok": True, "found": False, "name": name, "summary": f"no sighting of {name} in Annie's memory; try find_person or look_for",
                "known": sorted({str(e.get("identity") or e.get("label")) for e in entities if e.get("identity") or e.get("label")})[:20]}
    e = min(hits, key=lambda h: h.get("age_s") if isinstance(h.get("age_s"), (int, float)) else 1e12)
    label = e.get("identity") or e.get("label") or e.get("name")
    age = e.get("age_s")
    ago = "at an unknown time" if not isinstance(age, (int, float)) else f"{age:.0f} s ago" if age < 90 else f"{age / 60:.0f} min ago"
    place = f" in {e['place_name']}" if e.get("place_name") else ""
    return {"ok": True, "found": True, "name": label, "kind": e.get("kind"), "x": e.get("x"), "y": e.get("y"), "frame": "odom (metres)",
            "place": e.get("place"), "place_name": e.get("place_name"), "age_s": age, "last_seen": e.get("last_seen"),
            "posture": e.get("posture"), "n_seen": e.get("n_seen"),
            "summary": f"{label} was last seen {ago}{place} at x={e.get('x')}, y={e.get('y')} (a perception estimate)"}


@mcp.tool()
async def people() -> dict:
    """The people Annie knows by name (enrolled through the family app): name, relation, shirt colour, faces."""
    got = await client().request("GET", "/people")
    if not got["ok"]:
        return got
    data = _object_data(got, "a people list")
    if not got["ok"]:
        return data
    return {"ok": True, "people": data.get("people") or []}


@mcp.tool()
async def command_status(command_id: str) -> dict:
    """The receipt of a command sent earlier (accepted, queued, executing, completed, failed or cancelled)."""
    cid = _text(command_id, 80)
    if not cid or "/" in cid:
        return {"ok": False, "error": "command_id must be the id a tool returned"}
    got = await client().request("GET", f"/command/{cid}")
    return _receipt(got["data"], cid) if got["ok"] and isinstance(got["data"], dict) else got


# ---------------------------------------------------------------------------------------------------------------
# commands (these move or speak through the real robot)
# ---------------------------------------------------------------------------------------------------------------
@mcp.tool()
async def stop() -> dict:
    """Stop the dog now: cancel the running mission and hold still. A SOFTWARE stop, not a hardware emergency stop."""
    cancel = await client().request("POST", "/stop", {})
    hold = await client().request("POST", "/command", {"action": "stop"})
    if not cancel["ok"] and not hold["ok"]:
        return {**(cancel if cancel.get("error") == OFFLINE else hold), "note": "STOP WAS NOT DELIVERED. " + STOP_NOTE}
    return {"ok": True, "mission_cancelled": cancel["data"] if cancel["ok"] else cancel.get("error"),
            "hold": hold["data"] if hold["ok"] else hold.get("error"), "note": STOP_NOTE}


@mcp.tool()
async def instruct(text: str) -> dict:
    """Tell Annie what to do in plain language ('check on Jeanine', 'remind Jeanine to drink water', 'go to the door').
    The dog plans it into validated steps (elder-care skills included) and runs them; returns the receipt to poll."""
    line = _text(text, 300)
    if not line:
        return {"ok": False, "error": "text must be 1-300 characters"}
    return await client().submit("instruct", {"text": line, "author": "mcp"})


@mcp.tool()
async def command(action: str) -> dict:
    """One fixed action: explore, scan, go_home, stop, hello, dance, sit, stand or follow."""
    act = (action or "").strip().lower()
    if act not in ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}: use one of {', '.join(ACTIONS)}"}
    got = await client().request("POST", "/command", {"action": act})
    if not got["ok"]:
        return got
    return {"ok": True, "accepted": act, "note": STOP_NOTE if act == "stop" else "accepted by the patrol loop; this is not proof it was carried out"}


@mcp.tool()
async def say(text: str) -> dict:
    """Speak a short line through the dog's speaker (max 300 characters). Waits up to 30 s for it to finish."""
    line = _text(text, 300)
    if not line:
        return {"ok": False, "error": "text must be 1-300 characters"}
    return await client().submit("say", {"text": line}, wait_s=30.0)


@mcp.tool()
async def listen(max_s: float = 8.0) -> dict:
    """Listen for a spoken reply (1-15 s) and return the transcript in result.transcript when something was heard."""
    try:
        seconds = _bounded_number(max_s, 1.0, 15.0, "max_s")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return await client().submit("listen", {"max_s": seconds}, wait_s=seconds + 20.0)


@mcp.tool()
async def find_person(name: str | None = None, approach: bool = True) -> dict:
    """Search for a person and (approach=true) walk up to them. Name someone Annie knows, or leave empty for the
    nearest person. Waits for the outcome, at most 120 s."""
    return await client().submit("find_person", {"name": _text(name, 80), "approach": bool(approach), "timeout_s": 90.0}, wait_s=120.0)


@mcp.tool()
async def look_for(thing: str) -> dict:
    """Scan the room with the camera for a thing or place (a door, the kitchen, a red cup) and walk toward it if seen.
    Sent as the instruction 'go to the <thing>' (the dog has no direct look_for command). Waits at most 120 s."""
    what = _text(thing, 60)
    if not what:
        return {"ok": False, "error": "thing must be 1-60 characters"}
    return await client().submit("instruct", {"text": f"go to the {what}", "author": "mcp"}, wait_s=120.0)


@mcp.tool()
async def remember_person(name: str, relation: str | None = None, shirt: str | None = None) -> dict:
    """Add or update someone Annie should know by name (relation such as 'daughter', shirt colour for today).
    No photos over MCP: faces are enrolled from the family app."""
    who = _text(name, 40)
    if not who:
        return {"ok": False, "error": "name must be 1-40 letters"}
    payload = {"name": who, **{k: v for k, v in (("relation", _text(relation, 40)), ("shirt", _text(shirt, 20))) if v}}
    got = await client().request("POST", "/people", payload)
    return {"ok": True, "person": got["data"]} if got["ok"] else got


@mcp.tool()
async def voice_settings(cloud: bool | None = None, input_device: str | None = None, output_device: str | None = None) -> dict:
    """Read the voice setup, or change it: cloud voices on/off, microphone and speaker by device name. With no
    arguments it only reads. API keys are never sent or returned here."""
    payload = {k: v for k, v in (("cloud", cloud if isinstance(cloud, bool) else None), ("input_device", _text(input_device, 80)),
                                 ("output_device", _text(output_device, 80))) if v is not None}
    got = await client().request("POST", "/voice", payload) if payload else await client().request("GET", "/voice")
    if not got["ok"]:
        return got
    data = got["data"] if isinstance(got["data"], dict) else {}
    return {"ok": True, "changed": sorted(payload), "voice": {k: v for k, v in data.items() if "key" not in k.lower()}}


# ---------------------------------------------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------------------------------------------
@mcp.resource("annie://status", name="status", description="Annie's live status (the dog_status tool as JSON)", mime_type="application/json")
async def status_resource() -> str:
    return json.dumps(await dog_status())


@mcp.resource("annie://people", name="people", description="The people Annie knows by name", mime_type="application/json")
async def people_resource() -> str:
    return json.dumps(await people())


if __name__ == "__main__":
    mcp.run()  # stdio
