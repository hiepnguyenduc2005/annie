"""The dog's situated agent: what is going on right now, natural-language instructions into skill steps, and
what is worth saying about the scene.

Everything here is pure and testable. Language-model calls go through `robot.dog.inference` (Henry's single
inference switch) and every plan is validated against the body contract before the dog process runs it, so a
bad model reply can only ever produce a shorter or empty plan, never an unknown command. Without a model (or
when it times out) `plan_instruction` falls back to keyword rules so the demo keeps working.

Situation -> the `situation()` dict: pose, people/objects with distance + bearing relative to the dog's nose,
their age, the under-cover state (from `SpacetimeGraph.overhead`), recent greetings and the graph's sentences.
"""
from __future__ import annotations

import json
import math
import re
import time

# Steps the agent may plan. Every name/arg is re-validated by `robot.dog.runtime.body.validate_command`.
SKILLS = {
    "find_person": "find_person(name?: string, approach?: bool, timeout_s?: number) - search for and walk up to a person; "
                   "name null = nearest person",
    "say": "say(text: string) - speak through the speaker (max 300 chars)",
    "listen": "listen(max_s?: number) - listen for a spoken reply (1-15 s) and report what was heard",
    "turn": "turn(degrees: number) - turn in place, positive = left, up to 360",
    "walk": "walk(metres: number) - walk straight, -1 to 3 m (negative = back up); stops early at obstacles",
    "hello": "hello() - wave a paw", "dance": "dance() - short dance", "stretch": "stretch()", "heart": "heart() - a heart gesture",
    "sit": "sit()", "stand": "stand()",
    "look_for": "look_for(thing: string) - scan the room with the camera for something not in the situation (a door, "
                "a window, the kitchen, a red cup) and walk toward it if seen; reports found/not found",
    "patrol": "patrol(duration_s: number) - explore on its own for a while",
    "go_home": "go_home() - walk back to where the patrol started (takes up to 90 s)",
    "stop": "stop() - stop moving",
}
MAX_STEPS = 8


# ---------------------------------------------------------------------------------------------------------------
# situation
# ---------------------------------------------------------------------------------------------------------------
def bearing_deg(pose_xy, yaw, x, y) -> float:
    """Direction of (x, y) relative to the dog's nose: 0 ahead, +90 left, -90 right, +-180 behind."""
    ang = math.atan2(y - pose_xy[1], x - pose_xy[0]) - yaw
    return math.degrees((ang + math.pi) % (2 * math.pi) - math.pi)


def clock_word(bearing: float) -> str:
    b = bearing
    if abs(b) <= 25:
        return "ahead"
    if abs(b) >= 150:
        return "behind"
    side = "left" if b > 0 else "right"
    return f"ahead-{side}" if abs(b) < 70 else (f"to the {side}" if abs(b) < 115 else f"behind-{side}")


def _ago(s: float) -> str:
    s = max(0.0, s)
    return "now" if s < 3 else f"{s:.0f} s ago" if s < 90 else f"{s / 60:.0f} min ago"


def situation(*, now, pose_xy, yaw, entities, graph_sentences=(), overhead=None, greeted=(), tracks=(), ranges=None,
              home_m=0.0, battery=None, mode="-", max_people=6, max_objects=8, horizon_s=600.0) -> dict:
    """Bounded, JSON-serialisable picture of the moment. `entities` are `SpacetimeGraph.snapshot()["entities"]`."""
    people, objects = [], []
    for e in entities or ():
        last = e.get("last_seen")
        if last is None or now - last > horizon_s or e.get("x") is None:
            continue
        d = math.hypot(e["x"] - pose_xy[0], e["y"] - pose_xy[1])
        b = bearing_deg(pose_xy, yaw, e["x"], e["y"])
        row = {"name": e.get("identity") or e.get("label") or e.get("kind"), "kind": e.get("kind"), "identity": e.get("identity"),
               "distance_m": round(d, 1), "bearing_deg": round(b), "where": clock_word(b), "age_s": round(now - last, 1),
               "posture": e.get("posture"), "x": e["x"], "y": e["y"]}
        (people if e.get("kind") == "person" else objects).append(row)
    people.sort(key=lambda r: (0 if r["identity"] else 1, r["age_s"]))
    objects.sort(key=lambda r: r["age_s"])
    in_view = [{"track_id": t.get("track_id"), "name": ((t.get("identity") or {}).get("name")), "posture": t.get("posture")} for t in tracks or ()]
    return {"t": now, "pose": [round(pose_xy[0], 2), round(pose_xy[1], 2), round(math.degrees(yaw))], "mode": mode,
            "battery": battery, "home_m": round(home_m, 1), "ranges": ranges,
            "people_in_view": in_view, "people": people[:max_people], "objects": objects[:max_objects],
            "under_cover": overhead or {"covered": False},
            "recent_greetings": [dict(g) for g in list(greeted)[-4:]], "memory": list(graph_sentences)[:8]}


def describe(sit: dict) -> str:
    """The situation as short lines for a prompt."""
    pose = sit.get("pose") or [0, 0, 0]
    lines = [f"pose x={pose[0]} y={pose[1]} heading={pose[2]} deg, mode={sit.get('mode', '-')}, "
             f"{len(sit.get('people_in_view') or [])} people in camera view, {sit.get('home_m', 0)} m from home"]
    r = sit.get("ranges") or {}
    if r:
        fmt = lambda v: "clear" if v is None or v == float("inf") else f"{v:.1f} m"  # noqa: E731
        lines.append(f"lidar: front {fmt(r.get('front'))}, left {fmt(r.get('left'))}, right {fmt(r.get('right'))}")
    uc = sit.get("under_cover") or {}
    if uc.get("covered"):
        lines.append(f"UNDER something low (table/desk: {uc.get('voxels', '?')} voxels overhead); entered {_ago(uc.get('since_s', 0))} "
                     f"heading {uc.get('entry_heading_deg', '?')} deg; the way out is to reverse or turn toward {uc.get('exit_heading_deg', '?')} deg")
    for p in sit.get("people") or []:
        who = p["name"] if p["identity"] else f"unknown person ({p['name']})"
        lines.append(f"person: {who} {p['distance_m']} m {p['where']} (bearing {p['bearing_deg']}), seen {_ago(p['age_s'])}"
                     + (f", {p['posture']}" if p.get("posture") and p["posture"] != "unknown" else ""))
    for o in sit.get("objects") or []:
        lines.append(f"object: {o['name']} {o['distance_m']} m {o['where']} (bearing {o['bearing_deg']}), seen {_ago(o['age_s'])}")
    for g in sit.get("recent_greetings", []):
        lines.append(f"greeted: {g.get('name') or 'someone'} {_ago(sit['t'] - g.get('t', sit['t']))}")
    lines += [f"memory: {m}" for m in sit.get("memory", [])]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# instructions -> steps
# ---------------------------------------------------------------------------------------------------------------
PERSONA = (
    "Annie is a small robot dog who lives with Jeanine, an older woman, and looks after her for the family. Annie is "
    "caring, warm and unhurried: she notices how people seem (tired, cheerful, unsteady), asks gently, uses first names, "
    "remembers what she saw earlier and mentions it naturally, keeps sentences short and kind, and never sounds like a "
    "machine (no 'processing', 'detected', 'command', 'task'). She is honest when she cannot do or find something."
)

SYSTEM_PROMPT = (
    PERSONA + "\nYou control Annie. You get the current "
    "situation and one instruction from a family member or operator. Reply with JSON only: "
    '{"reply": "<one short sentence Annie says or reports>", "steps": [{"name": "...", "args": {...}}, ...]}. '
    "Available steps:\n" + "\n".join(f"- {v}" for v in SKILLS.values()) +
    "\nRules: at most 8 steps; use the situation (bearings, who was seen where and when) instead of guessing; "
    "'behind you' means turn about 180 degrees first; greet = find_person then hello then say; never invent people; "
    "for a place or thing that is not listed in the situation (door, window, kitchen, table) use look_for, not guessed turns; "
    "if the instruction is unclear or unsafe, reply with an empty steps list and say why in reply."
)

_NAMES = {"jeanine": "Jeanine", "grandma": "Jeanine", "granny": "Jeanine", "nana": "Jeanine", "gran": "Jeanine"}


def _first_name(text: str):
    for word, name in _NAMES.items():
        if re.search(rf"\b{word}\b", text, re.I):
            return name
    m = re.search(r"\b(?:find|greet|to|at|on|for|check on)\s+([A-Z][a-z]{2,})\b", text)
    return m.group(1) if m else None


def rule_plan(text: str, sit: dict | None = None) -> dict:
    """Keyword fallback: a handful of instructions that must work without any model."""
    t = text.strip()
    low = t.lower()
    name = _first_name(t)
    steps, reply = [], ""
    if re.search(r"\b(turn|look) (around|back|behind)\b|\bbehind you\b", low):
        steps.append({"name": "turn", "args": {"degrees": 180}})
    elif m := re.search(r"\bturn (left|right)( \d+)?", low):
        deg = int(m.group(2)) if m.group(2) else 90
        steps.append({"name": "turn", "args": {"degrees": deg if m.group(1) == "left" else -deg}})
    if m := re.search(r"\b(?:walk|go|move) (forward|ahead|back|backwards?)(?: (\d+(?:\.\d+)?) ?m)?\b(?! and)", low):
        dist = float(m.group(2)) if m.group(2) else 1.0
        steps.append({"name": "walk", "args": {"metres": -min(dist, 1.0) if m.group(1).startswith("back") else min(dist, 3.0)}})
    if re.search(r"\b(stop|halt|freeze)\b", low):
        return {"reply": "Stopping.", "steps": [{"name": "stop", "args": {}}], "source": "rules"}
    if re.search(r"\b(go|come|walk|head) (back )?home\b|\bhome base\b", low):
        return {"reply": "Heading home.", "steps": steps + [{"name": "go_home", "args": {}}], "source": "rules"}
    if m := re.search(r"\b(?:go|walk|head|move) (?:to|towards|toward|through|into|over to) (?:the |a |my )?([a-z][a-z ]{1,40}?)(?:[.!,]| and\b|$)", low):
        thing = m.group(1).strip()
        if thing not in ("me", "you", "us", "him", "her", "them") and not _NAMES.get(thing) and thing != "home":
            steps.append({"name": "look_for", "args": {"thing": thing}})
            reply = f"Looking for the {thing}."
    if re.search(r"\b(explore|patrol|look around|wander)\b", low):
        return {"reply": reply or "Exploring.", "steps": steps + [{"name": "patrol", "args": {"duration_s": 60}}], "source": "rules"}
    if re.search(r"\b(sit|sit down)\b", low):
        steps.append({"name": "sit", "args": {}})
    elif re.search(r"\b(stand|stand up|get up)\b", low):
        steps.append({"name": "stand", "args": {}})
    if re.search(r"\b(check|are you (ok|okay|alright)|how (is|are))\b", low):
        who = name or "there"
        steps += [{"name": "find_person", "args": {"name": name}}, {"name": "say", "args": {"text": f"Hi {who}, are you alright?"}},
                  {"name": "listen", "args": {"max_s": 8}}]
        reply = f"Checking on {who}."
    elif m := re.search(r"\b(?:tell|say to|remind)\s+(\w+)\s+(?:to\s+|that\s+)?(.+)", t, re.I):
        target = _NAMES.get(m.group(1).lower(), m.group(1) if m.group(1)[0].isupper() else None)
        msg = m.group(2).strip().rstrip(".")
        steps += [{"name": "find_person", "args": {"name": target}}, {"name": "say", "args": {"text": f"Hi {target or 'there'}, {msg}."[:300]}},
                  {"name": "listen", "args": {"max_s": 8}}]
        reply = f"Telling {target or 'them'}: {msg}."
    elif re.search(r"\b(greet|say hi|say hello|wave|hello)\b", low):
        who = name or "the person"
        steps += [{"name": "find_person", "args": {"name": name}}, {"name": "hello", "args": {}},
                  {"name": "say", "args": {"text": f"Hi {name}, lovely to see you. How are you feeling?" if name else "Hello there, lovely to see you. How are you doing?"}}]
        reply = f"Going to greet {who}."
    elif re.search(r"\bdance\b", low):
        steps.append({"name": "dance", "args": {}})
        reply = "Dancing."
    elif re.search(r"\b(find|where is|where's|look for|locate)\b", low):
        who = name or "someone"
        steps.append({"name": "find_person", "args": {"name": name}})
        reply = f"Looking for {who}."
    elif m := re.search(r"^\s*say\s+(.+)", t, re.I):
        steps.append({"name": "say", "args": {"text": m.group(1).strip()[:300]}})
        reply = "Saying it."
    if not steps:
        return {"reply": "I did not understand that; try 'greet the person behind you', 'tell Jeanine to plug in her phone', "
                         "'check on Jeanine', 'turn around', 'go home' or 'explore'.", "steps": [], "source": "rules"}
    return {"reply": reply or "On it.", "steps": steps[:MAX_STEPS], "source": "rules"}


def parse_plan(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return None
    return {"reply": str(data.get("reply") or "")[:300], "steps": data["steps"]}


def validate_steps(steps, validate_command) -> tuple[list, list]:
    """Keep the steps the body accepts (via the body contract's validator); return (kept, rejected reasons)."""
    kept, rejected = [], []
    for step in list(steps)[:MAX_STEPS]:
        if not isinstance(step, dict) or not isinstance(step.get("name"), str):
            rejected.append("step is not an object with a name")
            continue
        name, args = step["name"], step.get("args") or {}
        if name == "turn":
            deg = args.get("degrees")
            if not isinstance(deg, (int, float)) or isinstance(deg, bool) or not math.isfinite(deg) or not 0 < abs(deg) <= 360:
                rejected.append("turn: degrees must be a non-zero number up to 360")
                continue
            kept.append({"name": "turn", "args": {"degrees": float(deg)}})
            continue
        if name == "look_for":
            thing = args.get("thing")
            if not isinstance(thing, str) or not 1 <= len(thing.strip()) <= 60:
                rejected.append("look_for: thing must be 1-60 characters")
                continue
            kept.append({"name": "look_for", "args": {"thing": thing.strip()}})
            continue
        if name == "walk":
            m = args.get("metres")
            if not isinstance(m, (int, float)) or isinstance(m, bool) or not math.isfinite(m) or not -1.0 <= m <= 3.0 or m == 0:
                rejected.append("walk: metres must be a non-zero number in [-1, 3]")
                continue
            kept.append({"name": "walk", "args": {"metres": float(m)}})
            continue
        try:
            _, vname, vargs = validate_command({"name": name, "args": args})
        except ValueError as exc:
            rejected.append(f"{name}: {exc}")
            continue
        kept.append({"name": vname, "args": vargs})
    return kept, rejected


def plan_instruction(text: str, sit: dict, *, inference=None, validate_command=None, timeout_note=True) -> dict:
    """Instruction + situation -> {"reply", "steps", "source", "rejected", "latency_ms"}; never raises."""
    text = (text or "").strip()[:300]
    if not text:
        return {"reply": "Say what you want Annie to do.", "steps": [], "source": "none", "rejected": []}
    out, latency = None, None
    if inference is not None:
        try:
            resp = inference.chat([{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": f"Situation:\n{describe(sit)}\n\nInstruction: {text}\nJSON only."}],
                                  max_tokens=400, temperature=0.1)
            latency = resp.get("latency_ms")
            if resp.get("ok"):
                out = parse_plan(resp.get("text", ""))
                if out is not None:
                    out["source"] = f"{resp.get('provider', 'model')}:{resp.get('model', '')}"
        except Exception:
            out = None
    if out is None:
        out = rule_plan(text, sit)
    rejected = []
    if validate_command is not None:
        out["steps"], rejected = validate_steps(out["steps"], validate_command)
    if out.get("source", "") != "rules":  # small models drop the second half of an instruction: the rules fill it in
        rules = rule_plan(text, sit)
        have = {st["name"] for st in out["steps"]}
        social = [st for st in rules["steps"] if st["name"] in ("find_person", "hello", "say", "listen", "dance", "sit", "stand", "go_home", "patrol")]
        if social and not have & {st["name"] for st in social}:
            extra, _ = validate_steps(social, validate_command) if validate_command is not None else (social, [])
            out["steps"] = (out["steps"] + extra)[:MAX_STEPS]
            out["source"] += "+rules"
    if not out["steps"] and out.get("source", "").startswith(("local", "openrouter", "gemini", "openai")) and rejected:
        fb = rule_plan(text, sit)  # the model's plan was all invalid: the rules may still cover it
        if validate_command is not None:
            fb["steps"], _ = validate_steps(fb["steps"], validate_command)
        if fb["steps"]:
            out = fb
    out["rejected"], out["latency_ms"] = rejected, latency
    return out


# ---------------------------------------------------------------------------------------------------------------
# look_for: ask the vision model about one picture
# ---------------------------------------------------------------------------------------------------------------
SPOT_PROMPT = ("You look through a small robot dog's camera, low to the floor. Answer in JSON only: "
               '{"seen": true|false, "where": "left"|"centre"|"right", "note": "<5 words>"}. '
               "seen is true only if the thing is clearly visible in this picture.")


def parse_spot(text: str) -> dict:
    """{"seen": bool, "where": left|centre|right|None} from the model's reply; anything odd = not seen."""
    if not isinstance(text, str):
        return {"seen": False, "where": None}
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    seen = d.get("seen") is True or (isinstance(d.get("seen"), str) and d["seen"].strip().lower() == "true")
    where = str(d.get("where") or "").strip().lower().replace("center", "centre").replace("middle", "centre")
    if where not in ("left", "centre", "right"):
        where = "centre" if seen else None
    return {"seen": bool(seen), "where": where, "note": str(d.get("note") or "")[:60]}


def spot(thing: str, jpeg: bytes | None, *, inference=None) -> dict:
    """Is `thing` in this picture, and where? Uses the shared (local by default) vision client; never raises."""
    if not jpeg:
        return {"seen": False, "where": None, "error": "no frame"}
    try:
        if inference is None:
            from robot.dog.inference import shared
            inference = shared()
        resp = inference.chat([{"role": "system", "content": SPOT_PROMPT},
                               {"role": "user", "content": f"Is there a {thing} in this picture? JSON only."}],
                              images=[jpeg], max_tokens=60, temperature=0.0)
    except Exception as exc:
        return {"seen": False, "where": None, "error": type(exc).__name__}
    if not resp.get("ok"):
        return {"seen": False, "where": None, "error": resp.get("error"), "latency_ms": resp.get("latency_ms")}
    return {**parse_spot(resp.get("text", "")), "latency_ms": resp.get("latency_ms")}


# ---------------------------------------------------------------------------------------------------------------
# greeting with memory, and remarks about the scene
# ---------------------------------------------------------------------------------------------------------------
def greeting_decision(track, sit: dict, *, now, recent_s=300.0) -> dict:
    """Greet, re-greet with context, or stay quiet. The graph remembers a person at a spot or by name even when
    the tracker gives them a new id every few seconds."""
    ident = ((track.get("identity") or {}).get("name"))
    for g in sit.get("recent_greetings", []):
        if now - g.get("t", -1e9) > recent_s:
            continue
        if ident and g.get("name") == ident:
            return {"greet": False, "text": None, "reason": f"{ident} greeted {_ago(now - g['t'])}"}
        if not ident and g.get("x") is not None and track.get("world") is not None:
            if math.hypot(g["x"] - track["world"][0], g["y"] - track["world"][1]) < 1.2:
                return {"greet": False, "text": None, "reason": f"someone greeted at this spot {_ago(now - g['t'])}"}
    near_obj = None
    if track.get("world") is not None:
        for o in sit.get("objects", []):
            if math.hypot(o["x"] - track["world"][0], o["y"] - track["world"][1]) < 1.0:
                near_obj = o["name"]
                break
    if ident:
        text = f"Hi {ident}!" + (f" Sitting by the {near_obj}?" if near_obj else "")
    else:
        text = "Hello there, lovely to see you." + (f" I see you by the {near_obj}." if near_obj else "") + " How are you doing?"
    return {"greet": True, "text": text, "reason": "new to me here"}


LINE_PROMPT = (
    PERSONA + "\nWrite ONE short, natural sentence (max 18 words) that Annie says out loud right now, in her voice. "
    "Use the situation: the person's name if known, where they are, an object next to them, when you last saw them, "
    "how they seem, what you have been doing. Never mention robots, sensors, tracks, ids or confidence. "
    "Plain text only, no quotes, no emoji."
)


_BAD_LINE = re.compile(r"\b(about to|going to say|say hello|i see you\b|i can see you|detect|process|robot|camera|sensor|track|"
                       r"as an ai|language model|hello there!?$|greet(ing)? you)\b", re.I)


def line_ok(text: str, *, name: str | None = None) -> bool:
    """A model line Annie may say: no narration of her own mechanics, no meta talk, uses the name when known,
    ends like a sentence, and is not a bare 'hello'."""
    t = (text or "").strip()
    if len(t.split()) < 3 or _BAD_LINE.search(t):
        return False
    if name and name.lower() not in t.lower():
        return False
    return t[-1] in ".?!" or len(t.split()) >= 5


def compose_line(purpose: str, sit: dict, *, inference=None, fallback: str, extra: str = "", max_words=18,
                 name: str | None = None) -> dict:
    """A situated spoken line from the model (bounded and quality-gated by `line_ok`), else `fallback`.
    Returns {"text", "source", "latency_ms"}."""
    if inference is not None:
        try:
            resp = inference.chat([{"role": "system", "content": LINE_PROMPT},
                                   {"role": "user", "content": f"Situation:\n{describe(sit)}\n\nPurpose: {purpose}\n{extra}\nOne sentence:"}],
                                  max_tokens=60, temperature=0.7)
            text = (resp.get("text") or "").strip().strip('"').splitlines()[0].strip() if resp.get("ok") else ""
            words = text.split()
            if 2 <= len(words) <= max_words + 6 and not re.search(r"[{}\[\]<>]", text) and line_ok(text, name=name):
                return {"text": " ".join(words[:max_words + 6]), "source": f"{resp.get('provider')}:{resp.get('model')}",
                        "latency_ms": resp.get("latency_ms")}
        except Exception:
            pass
    return {"text": fallback, "source": "rules", "latency_ms": None}


_CONCERN = re.compile(r"\b(help|hurt|pain|fell|fallen|dizzy|can'?t (get up|breathe|move)|not (ok|okay|well|good|fine)|"
                      r"sick|ill|chest|ambulance|emergency|bleeding|scared)\b", re.I)
_FINE = re.compile(r"\b(fine|good|great|okay|ok|alright|all right|well|lovely|not bad|better)\b", re.I)


def classify_reply(heard: str | None) -> str:
    """'concern' | 'fine' | 'other' | 'none' from what the person said after Annie's question."""
    if not heard or not heard.strip():
        return "none"
    if _CONCERN.search(heard):
        return "concern"
    if _FINE.search(heard):
        return "fine"
    return "other"


def reply_fallback(kind: str, who: str | None) -> str:
    name = who or "dear"
    return {"concern": f"I'm right here with you, {name}. I'm letting the family know now.",
            "fine": f"That's lovely to hear, {name}. I'll be nearby if you need anything.",
            "other": f"Thank you for telling me, {name}. I'm listening.",
            "none": ""}[kind]


def converse_reply(heard: str | None, sit: dict, *, who: str | None, inference=None) -> dict:
    """Annie's spoken reply to what the person said: {"text", "kind", "source"}; a concern is never softened."""
    kind = classify_reply(heard)
    fallback = reply_fallback(kind, who)
    if kind == "none":
        return {"text": "", "kind": kind, "source": "rules"}
    if kind == "concern":
        return {"text": fallback, "kind": kind, "source": "rules"}  # fixed wording for safety
    line = compose_line(f"reply warmly to what {who or 'the person'} just said, in one sentence", sit, inference=inference,
                        fallback=fallback, extra=f"They said: \"{heard.strip()[:200]}\"", name=None)
    return {"text": line["text"], "kind": kind, "source": line["source"]}


class Narrator:
    """Occasional remarks about things the dog has just learned (new object placed, a known person back in view).
    Rate limited; never repeats an entity. Text only: the caller speaks it."""

    def __init__(self, *, min_gap_s=40.0, first_gap_s=15.0):
        self.min_gap_s, self.first_gap_s = min_gap_s, first_gap_s
        self.last_t = None
        self.said = set()
        self.start = None

    def remark(self, sit: dict, *, now) -> str | None:
        if self.start is None:
            self.start = now
        if now - self.start < self.first_gap_s or (self.last_t is not None and now - self.last_t < self.min_gap_s):
            return None
        for o in sit.get("objects", []):
            key = ("object", o["name"], round(o["x"]), round(o["y"]))
            if o["age_s"] <= 20 and key not in self.said and o["distance_m"] <= 3.0:
                self.said.add(key)
                self.last_t = now
                return f"Oh, a {o['name']} {o['where']}. I will remember that."
        for p in sit.get("people", []):
            if p["identity"] and p["age_s"] <= 5 and ("person", p["identity"]) not in self.said:
                self.said.add(("person", p["identity"]))
                self.last_t = now
                return f"There is {p['identity']}, {p['distance_m']} metres {p['where']}."
        return None


def now_s():
    return time.time()
