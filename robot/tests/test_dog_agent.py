"""The situated agent: situation building, instruction planning (rules + a fake model), greeting memory, remarks."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.dog.planning import agent  # noqa: E402
from robot.dog.runtime.body import validate_command  # noqa: E402

NOW = 1000.0


def _entities():
    return [{"kind": "person", "label": "person 7", "identity": "Jeanine", "x": 2.0, "y": 0.0, "last_seen": NOW - 20, "posture": "upright"},
            {"kind": "person", "label": "person 9", "identity": None, "x": -1.5, "y": 0.0, "last_seen": NOW - 5, "posture": "unknown"},
            {"kind": "object", "label": "chair", "identity": None, "x": 2.3, "y": 0.4, "last_seen": NOW - 60},
            {"kind": "object", "label": "door", "identity": None, "x": 0.0, "y": 3.0, "last_seen": NOW - 900}]  # too old


def test_situation_has_bearings_and_drops_stale():
    sit = agent.situation(now=NOW, pose_xy=(0.0, 0.0), yaw=0.0, entities=_entities(), graph_sentences=["a"], tracks=[{"track_id": 7}])
    assert [p["name"] for p in sit["people"]] == ["Jeanine", "person 9"]
    assert sit["people"][0]["where"] == "ahead" and sit["people"][1]["where"] == "behind"
    assert sit["objects"][0]["name"] == "chair" and len(sit["objects"]) == 1
    text = agent.describe(sit)
    assert "Jeanine 2.0 m ahead" in text and "behind" in text and "memory: a" in text


def test_describe_under_cover():
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=[], overhead={"covered": True, "voxels": 12, "since_s": 8, "entry_heading_deg": 135, "exit_heading_deg": -45})
    assert "UNDER something low" in agent.describe(sit) and "-45" in agent.describe(sit)


def test_rule_plan_behind_you_greet():
    plan = agent.plan_instruction("go back and greet the person behind you", agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=[]),
                                  validate_command=validate_command)
    names = [s["name"] for s in plan["steps"]]
    assert names == ["turn", "find_person", "hello", "say"] and plan["steps"][0]["args"]["degrees"] == 180.0
    assert plan["source"] == "rules" and plan["rejected"] == []


def test_rule_plan_relay_and_checkin_and_home():
    p = agent.plan_instruction("tell Grandma to plug in her phone", {}, validate_command=validate_command)
    assert [s["name"] for s in p["steps"]] == ["find_person", "say", "listen"] and p["steps"][0]["args"]["name"] == "Jeanine"
    assert "plug in her phone" in p["steps"][1]["args"]["text"]
    p = agent.plan_instruction("check on Jeanine", {}, validate_command=validate_command)
    assert p["steps"][1]["args"]["text"].startswith("Hi Jeanine, are you alright")
    p = agent.plan_instruction("go home", {}, validate_command=validate_command)
    assert [s["name"] for s in p["steps"]] == ["go_home"]
    p = agent.plan_instruction("do a backflip", {}, validate_command=validate_command)
    assert p["steps"] == [] and "not supported" in p["reply"]


class FakeInference:
    def __init__(self, text, ok=True):
        self.text, self.ok, self.calls = text, ok, []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return {"ok": self.ok, "text": self.text, "provider": "local", "model": "fake", "latency_ms": 5}


def test_model_plan_is_validated_and_bad_steps_dropped():
    inf = FakeInference('{"reply": "Turning to the door.", "steps": [{"name": "turn", "args": {"degrees": 40}}, '
                        '{"name": "walk", "args": {"metres": 2.5}}, {"name": "fly", "args": {}}, {"name": "walk", "args": {"metres": 9}}]}')
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=[{"kind": "object", "label": "door", "x": 2, "y": 1.7, "last_seen": NOW - 3}])
    plan = agent.plan_instruction("go out the door", sit, inference=inf, validate_command=validate_command)
    assert [s["name"] for s in plan["steps"]] == ["turn", "walk"] and plan["source"] == "local:fake"
    assert len(plan["rejected"]) == 2 and plan["reply"] == "Turning to the door."
    assert "door 2.6 m ahead-left" in inf.calls[0][1]["content"]


def test_model_plan_missing_the_social_half_is_completed_by_rules():
    inf = FakeInference('{"reply": "Turning.", "steps": [{"name": "turn", "args": {"degrees": 180}}]}')
    plan = agent.plan_instruction("turn around and greet the person behind you", {}, inference=inf, validate_command=validate_command)
    assert [s["name"] for s in plan["steps"]] == ["turn", "find_person", "hello", "say"] and plan["source"] == "local:fake+rules"


def test_model_recipient_only_greeting_keeps_wave_and_words():
    inf = FakeInference('{"reply": "I will wave at Grandma.", "steps": [{"name": "find_person", "args": {"name": "Jeanine"}}]}')
    plan = agent.plan_instruction("Go wave at Grandma", {}, inference=inf, validate_command=validate_command)
    assert [s["name"] for s in plan["steps"]] == ["find_person", "hello", "say"]
    assert plan["source"] == "local:fake+rules"
    assert plan["reply"] == "I will wave at Grandma."  # the model stays the sourced plan


def test_model_find_and_say_but_no_wave_still_gets_hello():
    inf = FakeInference('{"reply": "Hi Grandma!", "steps": [{"name": "find_person", "args": {"name": "Jeanine"}}, '
                        '{"name": "say", "args": {"text": "Hi Grandma!"}}]}')
    plan = agent.plan_instruction("go wave at grandma", {}, inference=inf, validate_command=validate_command)
    assert [s["name"] for s in plan["steps"]] == ["find_person", "hello", "say"]
    assert plan["source"] == "local:fake+rules"


def test_model_failure_falls_back_to_rules():
    plan = agent.plan_instruction("dance", {}, inference=FakeInference("", ok=False), validate_command=validate_command)
    assert [s["name"] for s in plan["steps"]] == ["dance"] and plan["source"] == "rules"
    plan = agent.plan_instruction("wave at Jeanine", {}, inference=FakeInference("not json at all"), validate_command=validate_command)
    assert plan["steps"][0]["name"] == "find_person" and plan["source"] == "rules"


def test_malformed_model_arguments_are_rejected_without_crashing():
    import json
    for args in ([], [1], "forward", True, 5):
        inf = FakeInference(json.dumps({"steps": [{"name": "walk", "args": args}]}))
        plan = agent.plan_instruction("dance", {}, inference=inf, validate_command=validate_command)
        assert plan["steps"] == [{"name": "dance", "args": {}}]
        assert any("args must be an object" in reason for reason in plan["rejected"])


def test_spot_requires_a_valid_direction_before_approaching():
    for response in ('{"seen": true}', '{"seen": true, "where": "unknown"}', '{"seen": true, "where": 17}'):
        assert agent.parse_spot(response) == {"seen": False, "where": None}


def test_greeting_decision_remembers_people_and_places():
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=_entities(),
                          greeted=[{"t": NOW - 60, "name": "Jeanine", "x": 2.0, "y": 0.0}, {"t": NOW - 30, "name": None, "x": -1.5, "y": 0.1}])
    d = agent.greeting_decision({"identity": {"name": "Jeanine"}, "world": (2.1, 0.1)}, sit, now=NOW)
    assert d["greet"] is False and "Jeanine greeted" in d["reason"]
    d = agent.greeting_decision({"identity": None, "world": (-1.4, 0.0)}, sit, now=NOW)
    assert d["greet"] is False and "this spot" in d["reason"]
    d = agent.greeting_decision({"identity": None, "world": (2.2, 0.3)}, sit, now=NOW)
    assert d["greet"] is False  # someone (Jeanine) was greeted at that spot a minute ago: probably her again
    d = agent.greeting_decision({"identity": None, "world": (2.8, 1.2)}, sit, now=NOW)
    assert d["greet"] is True and "chair" in d["text"]
    d = agent.greeting_decision({"identity": {"name": "Jeanine"}, "world": (2.1, 0.1)}, sit, now=NOW + 400)
    assert d["greet"] is True and d["text"].startswith("Hi Jeanine")


def test_compose_line_uses_model_or_fallback():
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=_entities())
    out = agent.compose_line("greet", sit, inference=FakeInference("Hello Jeanine, nice to see you by the chair again."), fallback="Hi Jeanine!")
    assert out["text"].startswith("Hello Jeanine") and out["source"] == "local:fake"
    out = agent.compose_line("greet", sit, inference=FakeInference('{"json": "no"}'), fallback="Hi Jeanine!")
    assert out["text"] == "Hi Jeanine!" and out["source"] == "rules"
    out = agent.compose_line("greet", sit, inference=None, fallback="Hi!")
    assert out["text"] == "Hi!"


def test_guest_identity_is_remembered_but_never_spoken_as_a_name():
    guest = {"identity": {"name": "Guest 2", "method": "guest"}, "world": (2, 0)}
    decision = agent.greeting_decision(guest, {}, now=NOW)
    assert decision["greet"] and "Guest" not in decision["text"]
    remembered = {"recent_greetings": [{"name": "Guest 2", "t": NOW - 10}]}
    assert not agent.greeting_decision(guest, remembered, now=NOW)["greet"]
    reply = agent.compose_line("greet", {}, inference=FakeInference("Hello Guest 2, how are you today?"),
                               fallback="Hello, how are you today?")
    assert reply["source"] == "rules" and "Guest" not in reply["text"]


def test_narrator_rate_limits_and_never_repeats():
    n = agent.Narrator(min_gap_s=40, first_gap_s=0)
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=[{"kind": "object", "label": "backpack", "x": 1, "y": 0, "last_seen": NOW - 2}])
    assert "backpack" in n.remark(sit, now=NOW)
    assert n.remark(sit, now=NOW + 10) is None
    assert n.remark(sit, now=NOW + 50) is None  # same backpack: said already


def test_look_for_rule_and_spot_parsing():
    from robot.dog.planning.agent import parse_spot, rule_plan, spot, validate_steps
    from robot.dog.runtime.body import validate_command
    plan = rule_plan("go towards the door and through it!")
    assert plan["steps"] == [{"name": "look_for", "args": {"thing": "door"}}]
    kept, rejected = validate_steps([{"name": "look_for", "args": {"thing": " window "}}, {"name": "look_for", "args": {}}], validate_command)
    assert kept == [{"name": "look_for", "args": {"thing": "window"}}] and rejected
    assert parse_spot('{"seen": true, "where": "Right", "note": "wooden door"}') == {"seen": True, "where": "right", "note": "wooden door"}
    assert parse_spot("Sure! {\"seen\": false}")["seen"] is False and parse_spot("nonsense")["where"] is None

    class FakeInference:
        def chat(self, messages, *, images=None, **kw):
            assert images and b"jpeg" in images[0] and "door" in messages[-1]["content"]
            return {"ok": True, "text": '{"seen": true, "where": "left"}', "latency_ms": 12}
    assert spot("door", b"jpeg-bytes", inference=FakeInference()) == {"seen": True, "where": "left", "note": "", "latency_ms": 12}
    assert spot("door", None)["error"] == "no frame"


def test_conversation_replies_and_concern_is_never_softened():
    from robot.dog.planning.agent import classify_reply, converse_reply, line_ok
    assert classify_reply("I'm fine thank you") == "fine" and classify_reply("I fell and I can't get up") == "concern"
    assert classify_reply("") == "none" and classify_reply("the weather is nice") == "other"

    class Flattering:
        def chat(self, messages, **kw):
            return {"ok": True, "text": "No worries at all, everything is great!", "latency_ms": 5, "provider": "local", "model": "m"}
    r = converse_reply("help, I fell", {"people": [], "objects": [], "sentences": []}, who="Jeanine", inference=Flattering())
    assert r["kind"] == "concern" and "help" in r["text"] and r["source"] == "rules"
    assert "letting the family know" not in r["text"]  # telemetry is not notification delivery
    r = converse_reply("I'm fine", {"people": [], "objects": [], "sentences": []}, who="Jeanine", inference=Flattering())
    assert r["kind"] == "fine" and r["text"].startswith("No worries")
    assert not line_ok("I see you, and I'm just about to say hello.") and not line_ok("Hello there!")
    assert line_ok("Hi Jeanine, lovely to see you by the window. How are you feeling?", name="Jeanine")
    assert not line_ok("Lovely to see you. How are you?", name="Jeanine")  # knows the name, must use it


@pytest.mark.parametrize("text", ["The person in front of you is grandma", "the person ahead is Grandma.",
                                  "that person is grandma", "everyone here is grandma",
                                  "the lady in view is my granny", "grandma is the person in front of you",
                                  "that's grandma", "she is grandma", "Annie, that person is grandma"])
def test_demo_grandma_identity_statement_is_acknowledged_without_motion(text):
    sit = {"demo_everyone_grandma": True}
    plan = agent.plan_instruction(text, sit, inference=None, validate_command=validate_command)
    assert plan["steps"] == [{"name": "say", "args": {"text": "For this demo, everyone I can see is Grandma."}}]
    assert plan["reply"] == "For this demo, everyone I can see is Grandma."


@pytest.mark.parametrize("text", ["is everyone grandma?", "who is grandma?", "do not assume everyone is grandma",
                                  "don't treat everyone as grandma", "walk forward", "go wave at Grandma",
                                  "wave at everyone", "maybe that is grandma"])
def test_demo_grandma_questions_motion_and_negation_do_not_acknowledge(text):
    sit = {"demo_everyone_grandma": True}
    plan = agent.plan_instruction(text, sit, inference=None, validate_command=validate_command)
    assert plan["reply"] != "For this demo, everyone I can see is Grandma."
    assert "assume everyone is Grandma" not in plan["reply"] and "is everyone Grandma" not in plan["reply"]


def test_demo_grandma_reply_requires_demo_flag():
    text = "The person in front of you is grandma"
    plan = agent.plan_instruction(text, {}, inference=None, validate_command=validate_command)
    assert plan["reply"] != "For this demo, everyone I can see is Grandma."


def test_situation_carries_demo_flag_and_describe_notes_it():
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=[], demo_everyone_grandma=True)
    assert sit["demo_everyone_grandma"] is True and "person is Grandma for this demo" in agent.describe(sit)
    sit = agent.situation(now=NOW, pose_xy=(0, 0), yaw=0, entities=[])
    assert sit["demo_everyone_grandma"] is False and "person is Grandma for this demo" not in agent.describe(sit)


@pytest.mark.parametrize("text", ["Flip", "Flip over", "stand up and flip", "Do a backflip", "roll over"])
def test_unsupported_trick_cannot_become_unrelated_model_motion(text):
    class WrongMotion:
        def chat(self, *args, **kwargs):
            pytest.fail("Unsupported acrobatics must be rejected before model planning")
    for plan in (agent.rule_plan(text), agent.plan_instruction(text, {}, inference=WrongMotion(),
                                                            validate_command=validate_command)):
        assert plan["steps"] == []
        assert "not supported" in plan["reply"]


def test_a_greeting_line_must_actually_greet():
    from robot.dog.planning.agent import compose_line, line_ok
    assert not line_ok("I'll be right there, just a moment, to see who's come near.", greeting=True)
    assert line_ok("Hello there, lovely to see you by the window. How are you?", greeting=True)

    class Vague:
        def chat(self, messages, **kw):
            return {"ok": True, "text": "I'll be right there, just a moment, to see who's come near.", "latency_ms": 3, "provider": "local", "model": "m"}
    out = compose_line("greet this person who is right in front of you", {"people": [], "objects": [], "sentences": []},
                       inference=Vague(), fallback="Hello there, lovely to see you. How are you doing?")
    assert out["source"] == "rules" and out["text"].startswith("Hello there")
