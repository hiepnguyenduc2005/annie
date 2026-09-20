"""Annie's dog commands as dimOS skills: the body-command contract exposed to a dimOS agent.

Each skill is a `dimos.skills.skills.AbstractSkill` (a pydantic model whose fields are
the tool arguments and whose `__call__` runs it), so `SkillLibrary.get_tools()` turns
them into function-calling tools for a dimOS agent, and `SkillLibrary.call(name, **args)`
runs one. Under the hood every skill POSTs the same body command that the family
errand uses (`contract/body_commands.md`), against either `robot/go2_body.py` or the
live patrol process (`robot/go2_patrol_greet.py`, port 8011), waits for the receipt to
finish and returns a one-line result for the agent.

Skills: FindPerson (by name, e.g. the person in the red shirt = Jeanine), Say, Listen,
Wave, Dance, Stretch, Sit, Stand, Explore, GoHome, Stop. Results are firmware
acknowledgments and perception estimates, never proof that the motion happened; the
guardrails (collision, stall, battery, leash, link) live in the body/patrol process.

Run from the dimOS venv:
  .cache/dimos/.venv/bin/python -c "from robot.dimos_skills import annie_skills; lib = annie_skills(); print(lib.get_tools())"
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
from pydantic import Field

from dimos.skills.skills import AbstractSkill, SkillLibrary

DEFAULT_BODY_URL = os.environ.get("ANNIE_BODY_URL", "http://127.0.0.1:8011")
TERMINAL = ("completed", "failed", "cancelled")


class BodyLink:
    """Thin client for the body-command contract (shared by all skills)."""

    def __init__(self, base_url=DEFAULT_BODY_URL, token=None, poll_s=0.5):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Body-Token": token} if token else {}
        self.poll_s = poll_s

    def run(self, name, args=None, deadline_s=120.0) -> dict:
        cid = str(uuid.uuid4())
        with httpx.Client(timeout=10.0, trust_env=False, headers=self.headers) as client:
            r = client.post(f"{self.base_url}/command", json={"command_id": cid, "name": name, "args": args or {}})
            if r.status_code == 409:
                return {"state": "failed", "error": "busy with another command"}
            if r.status_code >= 400:
                return {"state": "failed", "error": f"rejected ({r.status_code}): {r.text[:120]}"}
            receipt = r.json()
            t0 = time.monotonic()
            while receipt.get("state") not in TERMINAL and time.monotonic() - t0 < deadline_s:
                time.sleep(self.poll_s)
                receipt = client.get(f"{self.base_url}/command/{cid}").json()
        return receipt

    def stop(self) -> dict:
        with httpx.Client(timeout=5.0, trust_env=False, headers=self.headers) as client:
            return client.post(f"{self.base_url}/stop").json()


def _summary(receipt: dict) -> str:
    if receipt.get("state") != "completed":
        return f"{receipt.get('name', 'command')} {receipt.get('state')}: {receipt.get('error') or 'no detail'}"
    return f"{receipt.get('name', 'command')} completed: {receipt.get('result')}"


class AnnieSkill(AbstractSkill):
    """Base: every Annie skill holds a BodyLink (not a tool argument)."""

    def __init__(self, link: BodyLink | None = None, **data):
        super().__init__(**data)
        self._link = link or BodyLink()


class FindPerson(AnnieSkill):
    """Search for a person and walk up to them. Name a known person (e.g. "Jeanine") or leave empty for anyone."""
    person: str = Field("", description="Who to find (e.g. Jeanine); empty means the nearest person")
    timeout_s: float = Field(60.0, description="Give up after this many seconds")

    def __call__(self):
        return _summary(self._link.run("find_person", {"name": self.person or None, "timeout_s": self.timeout_s,
                                                       "approach": True}, deadline_s=self.timeout_s + 30))


class Say(AnnieSkill):
    """Speak a short line out loud to the person in front of the dog."""
    text: str = Field(..., description="What to say (max 300 characters)")

    def __call__(self):
        return _summary(self._link.run("say", {"text": self.text[:300]}, deadline_s=60))


class Listen(AnnieSkill):
    """Listen for a spoken reply and return the transcript."""
    max_s: float = Field(8.0, description="How long to listen, seconds (max 15)")

    def __call__(self):
        receipt = self._link.run("listen", {"max_s": min(15.0, self.max_s)}, deadline_s=30)
        if receipt.get("state") == "completed":
            res = receipt.get("result") or {}
            return f"heard: {res.get('transcript')!r}" if res.get("heard") else "heard nothing"
        return _summary(receipt)


def _trick(cls_name, command, doc):
    def __call__(self):
        return _summary(self._link.run(command, {}, deadline_s=60))
    return type(cls_name, (AnnieSkill,), {"__doc__": doc, "__call__": __call__, "__module__": __name__})


Wave = _trick("Wave", "hello", "Wave hello (the Go2 'hello' trick).")
Dance = _trick("Dance", "dance", "Do a short dance.")
Stretch = _trick("Stretch", "stretch", "Stretch.")
Sit = _trick("Sit", "sit", "Sit down.")
Stand = _trick("Stand", "stand", "Stand up.")


class Explore(AnnieSkill):
    """Explore the space on its own for a while (smart patrol with collision guardrails)."""
    duration_s: float = Field(60.0, description="Seconds to explore (max 300)")

    def __call__(self):
        return _summary(self._link.run("patrol", {"duration_s": min(300.0, self.duration_s)}, deadline_s=20))


class GoHome(AnnieSkill):
    """Return to the home base (where the patrol started)."""

    def __call__(self):
        with httpx.Client(timeout=5.0, trust_env=False, headers=self._link.headers) as client:
            r = client.post(f"{self._link.base_url}/command", json={"action": "go_home"})
        return "going home" if r.status_code < 300 else f"go_home rejected ({r.status_code})"


class Stop(AnnieSkill):
    """Stop moving now (software stop through the link; not a hardware emergency stop)."""

    def __call__(self):
        return f"stop sent: {self._link.stop()}"


SKILLS = (FindPerson, Say, Listen, Wave, Dance, Stretch, Sit, Stand, Explore, GoHome, Stop)


def annie_skills(link: BodyLink | None = None) -> SkillLibrary:
    """A dimOS SkillLibrary with every Annie skill bound to one body link.

    `lib.get_tools()` gives the function-calling tools; `lib.call("FindPerson", name="Jeanine")`
    instantiates the skill with the stored link and runs it (dimOS's own pattern, cf. Speak/tts_node).
    """
    link = link or BodyLink()
    lib = SkillLibrary()
    for cls in SKILLS:
        lib.add(cls)
        lib.create_instance(cls.__name__, link=link)
    return lib
