"""Elder-care skills for the planning loop: named compositions of the primitive steps in `agent.SKILLS`.

A care skill is a recipe, not a new body command. The planner (model or keyword rules) may name one
(`{"name": "check_on", "args": {"person": "Jeanine"}}`); `expand_steps` replaces it with primitive steps
(`find_person`, `say`, `listen`, `hello`, `look_for`, `patrol`, `go_home`) BEFORE the body validator runs,
so the dog process only ever executes commands it already knows. They mirror the dimOS `CareSkill` classes in
`robot/dog/dimos/agent.py` (CheckOnPerson, DeliverMessage, MedicationReminder, WhereIs, FindObject,
EscortToHome, NightCheck, WaveAtEveryone) and add the everyday reminders around them.

Pure and testable: no I/O, no clock, no model. `sit` is `agent.situation()` (or {}); memory answers
(`where_is`, `find_object`) read `sit["people"]` / `sit["objects"]`, which are perception estimates with an age.

Expansions are fixed when the plan is made, so a spoken line can never depend on the outcome of an earlier
step. Lines are therefore worded to stay true either way: a reminder is a reminder, never confirmation that
medication was taken; `fall_check` asks and listens, it does not assess; `escort_home` cannot tell whether the
person follows; `find_object` closes truthfully about the look it did, never that the thing was found; and
`night_check` says goodnight BEFORE the quiet walk, because the patrol command returns at once, so no line
after it may claim the walk finished or that the whole home was covered.
"""
from __future__ import annotations

MAX_STEPS = 8        # same bound as agent.MAX_STEPS: a plan is at most this many primitive steps
MAX_WAVE_ROUNDS = 3  # wave_at_everyone: find-and-wave rounds
_SAY_MAX = 300       # body contract: say(text) is 1-300 characters

_STR = {"type": "string", "required": True, "max": 80}
_OPT = {"type": "string", "required": False, "max": 80}
_TEXT = {"type": "string", "required": True, "max": 240}
_OPT_TEXT = {"type": "string", "required": False, "max": 120}


# ---------------------------------------------------------------------------------------------------------------
# primitive step builders
# ---------------------------------------------------------------------------------------------------------------
def _find(person=None, *, timeout_s=60.0):
    return {"name": "find_person", "args": {"name": person or None, "approach": True, "timeout_s": float(timeout_s)}}


def _say(text: str):
    text = " ".join(str(text).split())[:_SAY_MAX].strip()
    return {"name": "say", "args": {"text": text or "Hello."}}


def _listen(max_s=8.0):
    return {"name": "listen", "args": {"max_s": float(max_s)}}


def _step(name, **args):
    return {"name": name, "args": args}


def _hi(person) -> str:
    return f"Hi {person}" if person else "Hello there"


def _ago(age_s) -> str:
    try:
        s = max(0.0, float(age_s))
    except (TypeError, ValueError):
        return "a while ago"
    return "just now" if s < 10 else f"{s:.0f} seconds ago" if s < 90 else f"{s / 60:.0f} minutes ago"


def _recall(sit, name: str):
    """The freshest remembered person or object whose name contains `name`: (row, "person" | "object") or (None, None)."""
    want = (name or "").strip().lower()
    if not want:
        return None, None
    for key, kind in (("people", "person"), ("objects", "object")):
        rows = [r for r in (sit or {}).get(key) or [] if isinstance(r, dict)
                and want in " ".join(str(r.get(k) or "") for k in ("name", "identity")).lower()]
        if rows:
            return min(rows, key=lambda r: r.get("age_s") if isinstance(r.get("age_s"), (int, float)) else 1e9), kind
    return None, None


def _where(row) -> str:
    dist, where = row.get("distance_m"), row.get("where")
    bits = [f"about {dist:g} metres" if isinstance(dist, (int, float)) and not isinstance(dist, bool) else "", str(where or "")]
    return " ".join(b for b in bits if b) or "nearby"


# ---------------------------------------------------------------------------------------------------------------
# expansions: fn(args, sit) -> primitive steps. `args` are already cleaned by `clean_args`.
# ---------------------------------------------------------------------------------------------------------------
def _check_on(a, sit):
    return [_find(a.get("person")), _say(f"{_hi(a.get('person'))}, are you alright?"), _listen(8)]


def _deliver_message(a, sit):
    return [_find(a["person"]), _say(f"Hi {a['person']}, {a['text'].rstrip('.')}."), _listen(8)]


def _medication_reminder(a, sit):
    when = f"it is {a['time']}, " if a.get("time") else "it is "
    line = f"{a['person']}, {when}time to take your {a.get('medication') or 'medication'}. Please tell me when you have done it."
    return [_find(a["person"]), _say(line), _listen(10)]


def _where_is(a, sit):
    row, kind = _recall(sit, a["name"])
    if row is None:
        return [_say(f"I have not seen {a['name']} lately. I can go and look if you like.")]
    label = row.get("identity") or row.get("name") or a["name"]
    subject = label if kind == "person" else f"the {label}"
    return [_say(f"I last saw {subject} {_where(row)}, {_ago(row.get('age_s'))}.")]


def _find_object(a, sit):
    thing = a["thing"]
    row, kind = _recall(sit, thing)
    intro = (f"I remember seeing the {thing} {_where(row)}, {_ago(row.get('age_s'))}. Let me go and look."
             if row is not None and kind == "object" else f"Let me look around for the {thing}.")
    return [_say(intro), _step("look_for", thing=thing[:60]),
            _say(f"That is the end of my look for the {thing}. Tell me if you would like me to keep searching.")]


def _escort_home(a, sit):
    return [_find(a.get("person")), _say(f"{a.get('person') or 'Please'}, follow me. I will walk slowly."), _step("go_home")]


def _night_check(a, sit):
    # say goodnight FIRST: the patrol command completes immediately (it just starts the walk), so a line
    # after it could never truthfully say the round was done.
    return [_say("I am starting a quiet round. Sleep well."), _step("patrol", duration_s=60.0)]


def _wave_at_everyone(a, sit):
    people = [p for p in (sit or {}).get("people") or [] if isinstance(p, dict)]
    names = list(dict.fromkeys(p["identity"] for p in people if isinstance(p.get("identity"), str) and p["identity"]))
    targets = names[:MAX_WAVE_ROUNDS]
    if len(targets) < MAX_WAVE_ROUNDS and (not people or any(not p.get("identity") for p in people)):
        targets.append(None)  # someone without a name (or nobody remembered yet): the nearest person
    steps = []
    for who in targets:
        steps += [_find(who, timeout_s=30.0), _step("hello")]
    return steps + [_say("Hello everyone, lovely to see you all.")]


def _remind(a, sit):
    return [_find(a["person"]), _say(f"Hi {a['person']}, a gentle reminder: {a['text'].rstrip('.')}."), _listen(8)]


def _fetch_attention(a, sit):
    return [_find(a.get("person")), _step("hello"),
            _say(f"{_hi(a.get('person'))}, could you spare a moment? The family would like a word.")]


def _bedtime_reminder(a, sit):
    return [_find(a.get("person")), _say(f"{_hi(a.get('person'))}, it is getting late. Time to start getting ready for bed."), _listen(8)]


def _hydration_reminder(a, sit):
    return [_find(a.get("person")), _say(f"{_hi(a.get('person'))}, it is a good time for a glass of water."), _listen(8)]


def _fall_check(a, sit):
    person = a.get("person")
    if not person:  # someone the memory saw lying down is who to go to first
        person = next((p.get("identity") for p in (sit or {}).get("people") or []
                       if isinstance(p, dict) and p.get("posture") in ("lying", "fallen", "on_floor") and p.get("identity")), None)
    return [_find(person), _say(f"{person + ', are' if person else 'Are'} you alright? Do you need help? I can let the family know."),
            _listen(10)]


def _comfort(a, sit):
    who = a.get("person")
    return [_find(who), _say(f"{_hi(who)}, I am right here with you. Would you like to tell me how you are feeling?"), _listen(12)]


CARE_SKILLS = {
    "check_on": {"doc": "check_on(person?: string) - find the person, ask if they are alright and listen to the answer",
                 "args": {"person": _OPT}, "expand": _check_on},
    "deliver_message": {"doc": "deliver_message(person: string, text: string) - find the person, speak a family message and listen for a reply",
                        "args": {"person": _STR, "text": _TEXT}, "expand": _deliver_message},
    "medication_reminder": {"doc": "medication_reminder(person: string, medication?: string, time?: string) - find the person and "
                                   "remind them to take their medication (a reminder only, never confirmation it was taken)",
                            "args": {"person": _STR, "medication": _OPT_TEXT, "time": _OPT_TEXT}, "expand": _medication_reminder},
    "where_is": {"doc": "where_is(name: string) - say where a person or object was last seen, from memory (no moving)",
                 "args": {"name": _STR}, "expand": _where_is},
    "find_object": {"doc": "find_object(thing: string) - look around for an object (glasses, phone, keys) and walk to it if seen",
                    "args": {"thing": {"type": "string", "required": True, "max": 60}}, "expand": _find_object},
    "escort_home": {"doc": "escort_home(person?: string) - find the person, ask them to follow, and walk back to the home base",
                    "args": {"person": _OPT}, "expand": _escort_home},
    "night_check": {"doc": "night_check() - say goodnight, then take a quiet one-minute walk",
                    "args": {}, "expand": _night_check},
    "wave_at_everyone": {"doc": "wave_at_everyone() - go to each person remembered nearby (up to 3), wave, and say hello",
                         "args": {}, "expand": _wave_at_everyone},
    "remind": {"doc": "remind(person: string, text: string) - find the person and give a gentle reminder, then listen",
               "args": {"person": _STR, "text": _TEXT}, "expand": _remind},
    "fetch_attention": {"doc": "fetch_attention(person?: string) - find the person, wave, and ask for a moment of their attention",
                        "args": {"person": _OPT}, "expand": _fetch_attention},
    "bedtime_reminder": {"doc": "bedtime_reminder(person?: string) - find the person and remind them it is time for bed",
                         "args": {"person": _OPT}, "expand": _bedtime_reminder},
    "hydration_reminder": {"doc": "hydration_reminder(person?: string) - find the person and suggest a glass of water",
                           "args": {"person": _OPT}, "expand": _hydration_reminder},
    "fall_check": {"doc": "fall_check(person?: string) - walk up to the person (one seen lying down first), ask if they need help and "
                          "listen; it asks, it does not assess",
                   "args": {"person": _OPT}, "expand": _fall_check},
    "comfort": {"doc": "comfort(person?: string) - find the person, say something warm and listen",
                "args": {"person": _OPT}, "expand": _comfort},
}


# ---------------------------------------------------------------------------------------------------------------
# validation, expansion, prompt
# ---------------------------------------------------------------------------------------------------------------
def clean_args(name: str, args, aliases=None) -> dict:
    """Validated args for care skill `name`, or ValueError with a planner-facing reason. Unknown args are
    ignored (small models add them); a missing or mistyped declared arg is an error. `aliases` maps a lowercase
    nickname to the name the dog knows a person by ("grandma" -> "Jeanine")."""
    spec = CARE_SKILLS.get(name)
    if spec is None:
        raise ValueError(f"unknown care skill {name!r}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    out = {}
    for key, rule in spec["args"].items():
        value = args.get(key)
        if isinstance(value, str):
            value = " ".join(value.split())
        if value is None or value == "":
            if rule["required"]:
                raise ValueError(f"{key} is required")
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if len(value) > rule["max"]:
            raise ValueError(f"{key} must be at most {rule['max']} characters")
        if key == "person" and aliases:
            value = aliases.get(value.lower(), value)
        out[key] = value
    return out


def expand(name: str, args=None, sit=None, aliases=None) -> list:
    """Primitive steps for one care skill (ValueError on bad args). Always at most MAX_STEPS steps."""
    return CARE_SKILLS[name]["expand"](clean_args(name, args, aliases), sit or {})[:MAX_STEPS]


def expand_steps(steps, sit=None, aliases=None) -> list:
    """Replace care-skill steps with their primitives, keep everything else in order, and bound the plan at
    MAX_STEPS (later steps are dropped). A care step with bad args is left in place so the validator rejects it
    with a reason instead of it vanishing silently."""
    out = []
    for step in steps if isinstance(steps, (list, tuple)) else []:
        if len(out) >= MAX_STEPS:
            break
        name = step.get("name") if isinstance(step, dict) else None
        if isinstance(name, str) and name in CARE_SKILLS:
            try:
                out += expand(name, step.get("args"), sit, aliases)
                continue
            except ValueError:
                pass
        out.append(step)
    return out[:MAX_STEPS]


def skills_prompt() -> str:
    """One line per care skill for the planner's system prompt."""
    return "\n".join(f"- {spec['doc']}" for spec in CARE_SKILLS.values())
