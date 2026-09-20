"""Annie as a dimOS agent: elder-care skills, and the module/blueprint the dimOS agent consumes.

Two skill surfaces, one body contract
-------------------------------------
dimOS 0.0.14 (the pinned install) ships TWO skill APIs, and its agent only reads one:

1. Legacy: `dimos.skills.skills.AbstractSkill` + `SkillLibrary` (pydantic models,
   `get_tools()` -> OpenAI function-calling dicts). `robot/dog/missions/skills.py` uses it,
   and the elder-care skills below are `AbstractSkill` subclasses too. NO agent class in the
   installed dimOS accepts a `SkillLibrary` (only `skills/speak.py`, `kill_skill.py`,
   `rest/rest.py` and `robot/unitree/b1` still import it), so this surface is for our own
   callers (`lib.call(...)`) and for any function-calling model we drive ourselves.
2. Current: `@skill`-annotated methods on a `dimos.core.module.Module`. dimOS's `McpServer`
   collects them through `Module.get_skills()` and serves MCP `tools/list` / `tools/call`;
   the dimOS agent `McpClient` (a LangGraph agent) reads its tools from that one MCP URL.
   `AnnieSkillModule` is that surface: every method delegates to the same `SkillLibrary`,
   so both surfaces hit the identical body-command contract (`contract/body_commands.md`).

`annie_agent_blueprint()` wires AnnieSkillModule + McpServer + McpClient. It is BUILT in the
offline test and never started there: starting it binds the MCP port (9990 by default) and
needs a running model (Ollama daemon or an API key) and a body service.

Every skill result is a firmware acknowledgment, a transcript, or a perception estimate:
never proof that a motion happened, never a health assessment, never confirmation that
medication was taken. The guardrails stay in the dog process and win over any agent.
"""
from __future__ import annotations

import os
import time

import httpx
from pydantic import Field

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.module import Module, ModuleConfig
from dimos.skills.skills import SkillLibrary

from robot.dog.missions.skills import DEFAULT_BODY_URL, SKILLS, AnnieSkill, BodyLink, _summary

DEFAULT_VIEW_URL = os.environ.get("ANNIE_VIEW_URL", "")  # live view base URL serving /spacetime_latest.json
DEFAULT_MODEL = os.environ.get("ANNIE_DIMOS_MODEL", "ollama:qwen3:8b")  # any LangChain init_chat_model id

ANNIE_SYSTEM_PROMPT = (
    "You are Annie, a Unitree Go2 helper dog in an older adult's home. Use the tools to find people, speak, "
    "listen, check on them, deliver family messages and explore. Tool results are acknowledgments, transcripts "
    "and perception estimates, not proof and not medical facts. If someone may need help, say so plainly and "
    "tell the family; never diagnose. Keep spoken lines short and kind. Use Stop if anyone asks you to stop.")


class MemoryView:
    """Read-only client for the space-time memory's small live view (`/spacetime_latest.json`).

    `fetch` is injectable so tests and offline runs need no server. Without a URL or on any
    error `latest()` is None and the memory skills say the memory is unavailable.
    """

    def __init__(self, base_url: str = DEFAULT_VIEW_URL, fetch=None, timeout_s: float = 3.0):
        self.base_url, self._fetch, self.timeout_s = base_url.rstrip("/"), fetch, timeout_s

    def latest(self) -> dict | None:
        try:
            if self._fetch is not None:
                return self._fetch()
            if not self.base_url:
                return None
            with httpx.Client(timeout=self.timeout_s, trust_env=False) as client:
                r = client.get(f"{self.base_url}/spacetime_latest.json")
                return r.json() if r.status_code == 200 else None
        except Exception:
            return None


def _ok(receipt: dict) -> bool:
    return receipt.get("state") == "completed"


def _heard(receipt: dict) -> str | None:
    res = receipt.get("result") or {}
    return res.get("transcript") if _ok(receipt) and res.get("heard") else None


class CareSkill(AnnieSkill):
    """Base for the compositions: a BodyLink plus the memory view (neither is a tool argument)."""

    def __init__(self, link: BodyLink | None = None, memory: MemoryView | None = None, **data):
        super().__init__(link=link, **data)
        self._memory = memory or MemoryView()

    def _find(self, person: str, timeout_s: float) -> dict:
        return self._link.run("find_person", {"name": person or None, "timeout_s": timeout_s, "approach": True},
                              deadline_s=timeout_s + 30)

    def _say(self, text: str) -> dict:
        return self._link.run("say", {"text": text[:300]}, deadline_s=60)

    def _listen(self, max_s: float = 8.0) -> dict:
        return self._link.run("listen", {"max_s": min(15.0, max_s)}, deadline_s=30)

    def _go_home(self) -> str:
        """`go_home` is a patrol action, not a body command (same POST as the GoHome skill). A link may provide it."""
        if hasattr(self._link, "go_home"):
            return str(self._link.go_home())
        with httpx.Client(timeout=5.0, trust_env=False, headers=self._link.headers) as client:
            r = client.post(f"{self._link.base_url}/command", json={"action": "go_home"})
        return "going home" if r.status_code < 300 else f"go_home rejected ({r.status_code})"

    def _find_say_listen(self, person: str, line: str, timeout_s: float, what: str) -> str:
        found = self._find(person, timeout_s)
        if not _ok(found):
            return f"{what} not delivered: could not find {person or 'anyone'} ({_summary(found)})"
        said = self._say(line)
        if not _ok(said):
            return f"{what} not delivered: found {person or 'a person'} but speech failed ({_summary(said)})"
        reply = _heard(self._listen())
        return f"{what} spoken to {person or 'the person found'}; reply: {reply!r}" if reply else \
            f"{what} spoken to {person or 'the person found'}; no reply heard"


class CheckOnPerson(CareSkill):
    """Find a person, ask "are you alright?", listen, and return what they said. Not a health assessment."""
    person: str = Field("", description="Who to check on (e.g. Jeanine); empty means the nearest person")
    timeout_s: float = Field(60.0, description="Give up searching after this many seconds")

    def __call__(self):
        name = self.person or "there"
        return self._find_say_listen(self.person, f"Hi {name}, are you alright?", self.timeout_s, "check-in")


class DeliverMessage(CareSkill):
    """Find a person, speak a family message to them, and return their spoken reply."""
    person: str = Field(..., description="Who the message is for (e.g. Jeanine)")
    text: str = Field(..., description="The message to speak (max 300 characters)")
    timeout_s: float = Field(60.0, description="Give up searching after this many seconds")

    def __call__(self):
        return self._find_say_listen(self.person, self.text, self.timeout_s, "message")


class MedicationReminder(CareSkill):
    """Find a person and speak a medication reminder. Returns their reply; it cannot confirm the medication was taken."""
    person: str = Field(..., description="Who to remind")
    text: str = Field("it is time to take your medication", description="The reminder to speak")
    timeout_s: float = Field(60.0, description="Give up searching after this many seconds")

    def __call__(self):
        line = f"{self.person}, a reminder: {self.text}. Please tell me when you have done it."
        return self._find_say_listen(self.person, line, self.timeout_s, "medication reminder") + \
            " (a spoken reply is not confirmation that medication was taken)"


class WhereIs(CareSkill):
    """Where a person was last seen, from the space-time memory. A perception estimate with its age, not a live fix."""
    person: str = Field(..., description="Who to look up (e.g. Jeanine)")

    def __call__(self):
        latest = self._memory.latest()
        if latest is None:
            return "memory unavailable: the space-time view is not reachable, use FindPerson to search instead"
        want = self.person.strip().lower()
        for p in latest.get("people") or []:
            if want and want in str(p.get("identity") or "").lower():
                age = max(0.0, time.time() - float(latest.get("people_t") or time.time()))
                return (f"{p.get('identity')} was last seen at x={float(p.get('x', 0.0)):.1f} m, "
                        f"y={float(p.get('y', 0.0)):.1f} m (odometry frame), {age:.0f} s ago"
                        + (f", posture {p['posture']}" if p.get("posture") else ""))
        return f"no sighting of {self.person} in the latest memory view"


class FindObject(CareSkill):
    """Placeholder: report whether the memory summary mentions an object label. It does NOT search or drive."""
    label: str = Field(..., description="Object to look for (e.g. glasses, phone, keys)")

    def __call__(self):
        latest = self._memory.latest()
        if latest is None:
            return "memory unavailable: the space-time view is not reachable"
        want = self.label.strip().lower()
        hits = [e for e in latest.get("events") or [] if want and want in f"{e.get('kind', '')} {e.get('text', '')}".lower()]
        hits += [p for p in latest.get("people") or [] if want and want in str(p.get("label") or "").lower()]
        if not hits:
            return f"no record of {self.label!r} in the latest memory view (placeholder: object search is not implemented)"
        h = hits[-1]
        where = f" near x={float(h['x']):.1f} m, y={float(h['y']):.1f} m" if h.get("x") is not None and h.get("y") is not None else ""
        return f"memory mentions {self.label!r}{where}: {h.get('text') or h.get('label')}"


class EscortToHome(CareSkill):
    """Ask a person to follow, then walk back to the home base. The dog does not check that they are following."""
    person: str = Field("", description="Who to escort; empty means whoever is in front of the dog")

    def __call__(self):
        said = self._say(f"{self.person or 'Please'}, follow me. I will walk slowly.".strip())
        return f"escort: {'asked to follow' if _ok(said) else 'speech failed'}; {self._go_home()} (following is not verified)"


class NightCheck(CareSkill):
    """Quiet night round: explore for a while, then report who the memory saw. Absence of a sighting is not proof of absence."""
    duration_s: float = Field(90.0, description="Seconds to patrol (max 300)")

    def __call__(self):
        patrol = self._link.run("patrol", {"duration_s": min(300.0, self.duration_s)}, deadline_s=min(300.0, self.duration_s) + 30)
        latest = self._memory.latest()
        if latest is None:
            seen = "memory unavailable, no sighting report"
        else:
            people = [f"{p.get('identity') or p.get('label') or 'person'}" + (f" ({p['posture']})" if p.get("posture") else "")
                      for p in latest.get("people") or []]
            seen = "saw " + ", ".join(people) if people else "saw nobody in the latest view"
        return f"night check: {_summary(patrol)}; {seen}"


class WaveAtEveryone(CareSkill):
    """Find the nearest person and wave, up to a few rounds. It cannot tell people apart, so it may greet someone twice."""
    rounds: int = Field(3, description="How many find-and-wave rounds (max 5)")
    timeout_s: float = Field(30.0, description="Search time per round, seconds")

    def __call__(self):
        waved = 0
        for _ in range(max(1, min(5, self.rounds))):
            if not _ok(self._find("", self.timeout_s)):
                break
            if _ok(self._link.run("hello", {}, deadline_s=60)):
                waved += 1
        return f"waved {waved} time(s)"


CARE_SKILLS = (CheckOnPerson, DeliverMessage, MedicationReminder, WhereIs, FindObject, EscortToHome, NightCheck,
               WaveAtEveryone)


def eldercare_skills(link: BodyLink | None = None, memory: MemoryView | None = None) -> SkillLibrary:
    """The eleven body skills plus the eight elder-care compositions in one dimOS SkillLibrary."""
    link, memory = link or BodyLink(), memory or MemoryView()
    lib = SkillLibrary()
    # dimOS bug guard: `SkillLibrary._instances` is a CLASS attribute (dimos/skills/skills.py:107) and
    # `create_instance` never overwrites a key (:113), so without this the FIRST library built in a process
    # pins its link for every later library: a simulator or test library would drive the first (live) body.
    lib._instances = {}
    for cls in SKILLS:
        lib.add(cls)
        lib.create_instance(cls.__name__, link=link)
    for cls in CARE_SKILLS:
        lib.add(cls)
        lib.create_instance(cls.__name__, link=link, memory=memory)
    return lib


class AnnieSkillConfig(ModuleConfig):
    body_url: str = DEFAULT_BODY_URL
    view_url: str = DEFAULT_VIEW_URL


class AnnieSkillModule(Module):
    """The dimOS-native surface: `@skill` methods that McpServer serves and McpClient (the agent) calls.

    Movement skills declare `uses=[CAP_MOVEMENT]`, so dimOS's capability registry refuses a second
    movement tool while one is running (on top of the body service's own 409 busy).
    The body token comes from the ANNIE_BODY_TOKEN environment variable, never from config.
    """

    config: AnnieSkillConfig

    def __init__(self, link: BodyLink | None = None, memory: MemoryView | None = None, **kwargs):
        super().__init__(**kwargs)
        link = link or BodyLink(self.config.body_url, token=os.environ.get("ANNIE_BODY_TOKEN"))
        self._lib = eldercare_skills(link, memory or MemoryView(self.config.view_url))

    @skill(uses=[CAP_MOVEMENT])
    def find_person(self, person: str = "", timeout_s: float = 60.0) -> str:
        """Search for a person and walk up to them. Name a known person (e.g. Jeanine) or leave empty for anyone."""
        return str(self._lib.call("FindPerson", person=person, timeout_s=timeout_s))

    @skill
    def say(self, text: str) -> str:
        """Speak a short line out loud to the person in front of the dog (max 300 characters)."""
        return str(self._lib.call("Say", text=text))

    @skill
    def listen(self, max_s: float = 8.0) -> str:
        """Listen for a spoken reply and return the transcript."""
        return str(self._lib.call("Listen", max_s=max_s))

    @skill(uses=[CAP_MOVEMENT])
    def trick(self, name: str) -> str:
        """Do a trick: one of wave, dance, stretch, sit, stand."""
        cls = {"wave": "Wave", "dance": "Dance", "stretch": "Stretch", "sit": "Sit", "stand": "Stand"}.get(name.strip().lower())
        return str(self._lib.call(cls)) if cls else f"unknown trick {name!r}: use wave, dance, stretch, sit or stand"

    @skill(uses=[CAP_MOVEMENT])
    def explore(self, duration_s: float = 60.0) -> str:
        """Explore the space on its own for a while (smart patrol with collision guardrails, max 300 s)."""
        return str(self._lib.call("Explore", duration_s=duration_s))

    @skill(uses=[CAP_MOVEMENT])
    def go_home(self) -> str:
        """Return to the home base (where the patrol started)."""
        return str(self._lib.call("GoHome"))

    @skill
    def stop_moving(self) -> str:
        """Stop moving now (a software stop through the link; not a hardware emergency stop)."""
        return str(self._lib.call("Stop"))

    @skill(uses=[CAP_MOVEMENT])
    def check_on_person(self, person: str = "", timeout_s: float = 60.0) -> str:
        """Find a person, ask if they are alright, listen, and return what they said. Not a health assessment."""
        return str(self._lib.call("CheckOnPerson", person=person, timeout_s=timeout_s))

    @skill(uses=[CAP_MOVEMENT])
    def deliver_message(self, person: str, text: str) -> str:
        """Find a person, speak a family message to them, and return their spoken reply."""
        return str(self._lib.call("DeliverMessage", person=person, text=text))

    @skill(uses=[CAP_MOVEMENT])
    def medication_reminder(self, person: str, text: str = "it is time to take your medication") -> str:
        """Find a person and speak a medication reminder. The reply cannot confirm the medication was taken."""
        return str(self._lib.call("MedicationReminder", person=person, text=text))

    @skill
    def where_is(self, person: str) -> str:
        """Where a person was last seen according to the space-time memory (an estimate with its age)."""
        return str(self._lib.call("WhereIs", person=person))

    @skill
    def find_object(self, label: str) -> str:
        """Placeholder: report whether the memory summary mentions an object (does not search or drive)."""
        return str(self._lib.call("FindObject", label=label))

    @skill(uses=[CAP_MOVEMENT])
    def escort_to_home(self, person: str = "") -> str:
        """Ask a person to follow and walk back to the home base. Following is not verified."""
        return str(self._lib.call("EscortToHome", person=person))

    @skill(uses=[CAP_MOVEMENT])
    def night_check(self, duration_s: float = 90.0) -> str:
        """Quiet night round: explore, then report who the memory saw."""
        return str(self._lib.call("NightCheck", duration_s=duration_s))

    @skill(uses=[CAP_MOVEMENT])
    def wave_at_everyone(self, rounds: int = 3) -> str:
        """Find the nearest person and wave, for a few rounds (it may greet the same person twice)."""
        return str(self._lib.call("WaveAtEveryone", rounds=rounds))


def annie_agent_blueprint(model: str = DEFAULT_MODEL, body_url: str = DEFAULT_BODY_URL, view_url: str = DEFAULT_VIEW_URL,
                          system_prompt: str = ANNIE_SYSTEM_PROMPT):
    """dimOS blueprint: Annie's skills + dimOS's MCP server + dimOS's LangGraph agent (McpClient).

    Building it is side-effect free. RUNNING it (`blueprint.build()` via the dimOS coordinator)
    binds the MCP port (`global_config.mcp_port`, 9990), opens dimOS transports and needs the
    model to be reachable. Not run by the tests and not run in this change.

    TODO (Elastic MCP): `McpClient` reads tools from exactly ONE server,
    `McpClientConfig.mcp_server_url` (dimos/agents/mcp/mcp_client.py:86; `_mcp_request` posts
    to it at :138, `_fetch_tools` at :174 builds the whole tool list from its `tools/list`).
    There is no list of servers. To add an Elastic MCP server next to dimOS's own, subclass
    McpClient, add `extra_mcp_server_urls` (+ per-server headers for the Elastic API key, read
    from the environment) to the config, and override `_fetch_tools` to merge each server's
    `tools/list`, building each `StructuredTool` with a `call_tool` bound to ITS url (the stock
    `_mcp_tool_to_langchain` at :207 always calls `self._mcp_tool_call`, i.e. the single url).
    Simpler alternative with no dimOS change: a `@skill` on AnnieSkillModule that runs the
    Elasticsearch query itself (robot/dog/memory/elastic.py) and returns a short text summary.
    """
    from dimos.agents.mcp.mcp_client import McpClient
    from dimos.agents.mcp.mcp_server import McpServer
    from dimos.core.coordination.blueprints import autoconnect

    return autoconnect(
        AnnieSkillModule.blueprint(body_url=body_url, view_url=view_url),
        McpServer.blueprint(),
        McpClient.blueprint(model=model, system_prompt=system_prompt),
    )
