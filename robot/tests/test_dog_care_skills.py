"""Elder-care skills in the planning loop: every skill expands to primitives the body accepts, expansion is
bounded, care steps survive validation, and the keyword rules reach them without a model."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.dog.planning import agent, care_skills  # noqa: E402
from robot.dog.runtime.body import validate_command  # noqa: E402

NOW = 1000.0
ARGS = {  # one valid call per skill
    "check_on": {"person": "Jeanine"}, "deliver_message": {"person": "Jeanine", "text": "dinner is at six"},
    "medication_reminder": {"person": "Jeanine", "medication": "blood pressure tablets", "time": "eight o'clock"},
    "where_is": {"name": "Jeanine"}, "find_object": {"thing": "glasses"}, "escort_home": {"person": "Jeanine"},
    "night_check": {}, "wave_at_everyone": {}, "remind": {"person": "Jeanine", "text": "call Anna back"},
    "fetch_attention": {"person": "Jeanine"}, "bedtime_reminder": {"person": "Jeanine"},
    "hydration_reminder": {"person": "Jeanine"}, "fall_check": {}, "comfort": {"person": "Jeanine"},
}


def _sit():
    return agent.situation(now=NOW, pose_xy=(0.0, 0.0), yaw=0.0, entities=[
        {"kind": "person", "label": "person 7", "identity": "Jeanine", "x": 2.0, "y": 0.0, "last_seen": NOW - 20, "posture": "lying"},
        {"kind": "person", "label": "person 8", "identity": "Anna", "x": 0.0, "y": 2.0, "last_seen": NOW - 40, "posture": "upright"},
        {"kind": "person", "label": "person 9", "identity": None, "x": -1.5, "y": 0.0, "last_seen": NOW - 5, "posture": "unknown"},
        {"kind": "object", "label": "glasses", "identity": None, "x": 1.0, "y": 1.0, "last_seen": NOW - 120}])


def _names(steps):
    return [s["name"] for s in steps]


def test_at_least_twelve_skills_each_documented():
    assert len(care_skills.CARE_SKILLS) >= 12 and set(ARGS) == set(care_skills.CARE_SKILLS)
    for name, spec in care_skills.CARE_SKILLS.items():
        assert spec["doc"].startswith(name + "(") and callable(spec["expand"]) and isinstance(spec["args"], dict)
        assert name not in agent.SKILLS  # a care skill never shadows a primitive
    prompt = care_skills.skills_prompt()
    assert all(f"- {name}(" in prompt for name in care_skills.CARE_SKILLS) and prompt in agent.SYSTEM_PROMPT


@pytest.mark.parametrize("sit", [{}, None, "full"])
@pytest.mark.parametrize("name", sorted(ARGS))
def test_every_skill_expands_to_primitives_the_body_accepts(name, sit):
    steps = care_skills.expand(name, ARGS[name], _sit() if sit == "full" else sit)
    assert 1 <= len(steps) <= care_skills.MAX_STEPS
    assert all(s["name"] in agent.SKILLS for s in steps)  # primitives only, never another care skill
    kept, rejected = agent.validate_steps(steps, validate_command)
    assert rejected == [] and _names(kept) == _names(steps)


def test_expansions_have_the_documented_shape():
    sit = _sit()
    assert _names(care_skills.expand("check_on", {"person": "Jeanine"}, sit)) == ["find_person", "say", "listen"]
    assert _names(care_skills.expand("escort_home", {"person": "Jeanine"}, sit)) == ["find_person", "say", "go_home"]
    assert "follow me" in care_skills.expand("escort_home", {}, sit)[1]["args"]["text"].lower()
    night = care_skills.expand("night_check", {}, sit)
    assert _names(night) == ["say", "patrol"] and night[1]["args"]["duration_s"] == 60.0  # goodnight BEFORE the walk
    assert _names(care_skills.expand("fetch_attention", {"person": "Jeanine"}, sit)) == ["find_person", "hello", "say"]
    found = care_skills.expand("find_object", {"thing": "glasses"}, sit)
    assert "look_for" in _names(found) and _names(found)[-1] == "say" and "I remember seeing the glasses" in found[0]["args"]["text"]
    closing = found[-1]["args"]["text"]  # a fixed closing line cannot claim a result it cannot see
    assert "keep searching" in closing and "standing right next to" not in closing and "found" not in closing
    fall = care_skills.expand("fall_check", {}, sit)
    assert _names(fall) == ["find_person", "say", "listen"] and fall[0]["args"] == {"name": "Jeanine", "approach": True, "timeout_s": 60.0}
    assert "are you alright" in fall[1]["args"]["text"].lower()  # Jeanine was remembered lying down, so she is who it goes to
    med = care_skills.expand("medication_reminder", ARGS["medication_reminder"], sit)[1]["args"]["text"]
    assert "blood pressure tablets" in med and "eight o'clock" in med
    night = care_skills.expand("night_check", {}, sit)[0]["args"]["text"]
    assert "quiet round" in night and "sleep well" in night.lower()
    assert "finished" not in night  # the patrol command returns at once: no line may claim the walk completed


def test_where_is_answers_from_the_situation_without_moving():
    sit = _sit()
    person = care_skills.expand("where_is", {"name": "jeanine"}, sit)
    assert _names(person) == ["say"] and "Jeanine" in person[0]["args"]["text"] and "2 metres ahead" in person[0]["args"]["text"]
    thing = care_skills.expand("where_is", {"name": "glasses"}, sit)[0]["args"]["text"]
    assert "the glasses" in thing and "2 minutes ago" in thing
    unknown = care_skills.expand("where_is", {"name": "Bob"}, sit)
    assert _names(unknown) == ["say"] and "have not seen Bob" in unknown[0]["args"]["text"]
    assert _names(care_skills.expand("where_is", {"name": "Bob"}, {})) == ["say"]


def test_wave_at_everyone_is_bounded_and_uses_known_names():
    steps = care_skills.expand("wave_at_everyone", {}, _sit())
    assert _names(steps) == ["find_person", "hello"] * 3 + ["say"]
    assert [s["args"]["name"] for s in steps if s["name"] == "find_person"] == ["Jeanine", "Anna", None]
    assert _names(care_skills.expand("wave_at_everyone", {}, {})) == ["find_person", "hello", "say"]
    crowd = {"people": [{"identity": f"P{i}", "name": f"P{i}"} for i in range(9)]}
    assert len(care_skills.expand("wave_at_everyone", {}, crowd)) <= care_skills.MAX_STEPS


def test_bad_args_are_rejected_with_a_reason():
    for name, args in [("deliver_message", {"person": "Jeanine"}), ("remind", {"person": "", "text": "x"}), ("where_is", {}),
                       ("check_on", {"person": 7}), ("check_on", {"person": "x" * 81}), ("find_object", {"thing": "y" * 61}),
                       ("check_on", ["Jeanine"]), ("nonsense", {})]:
        with pytest.raises(ValueError):
            care_skills.clean_args(name, args)
    assert care_skills.clean_args("check_on", {"person": "  grandma ", "volume": 11}, {"grandma": "Jeanine"}) == {"person": "Jeanine"}
    assert care_skills.clean_args("check_on", None) == {}
    long = care_skills.expand("deliver_message", {"person": "Jeanine", "text": "a" * 240}, {})
    assert len(long[1]["args"]["text"]) <= 300


def test_expand_steps_is_bounded_and_keeps_order():
    steps = [{"name": "turn", "args": {"degrees": 180}}, {"name": "check_on", "args": {"person": "Jeanine"}},
             {"name": "wave_at_everyone", "args": {}}, {"name": "comfort", "args": {}}, {"name": "dance", "args": {}}]
    out = care_skills.expand_steps(steps, _sit())
    assert len(out) == care_skills.MAX_STEPS == agent.MAX_STEPS
    assert _names(out) == ["turn", "find_person", "say", "listen", "find_person", "hello", "find_person", "hello"]  # later ones dropped
    assert not any(s["name"] in care_skills.CARE_SKILLS for s in out)
    assert care_skills.expand_steps([{"name": "night_check"}] * 20, {}) == care_skills.expand_steps([{"name": "night_check"}] * 4, {})
    assert care_skills.expand_steps(None) == [] and care_skills.expand_steps("check_on") == []
    odd = care_skills.expand_steps(["check_on", {"name": "remind", "args": {"person": "Jeanine"}}, {"args": {}}], {})
    assert odd == ["check_on", {"name": "remind", "args": {"person": "Jeanine"}}, {"args": {}}]  # left for the validator to reject


def test_a_plan_using_check_on_survives_validate_steps():
    kept, rejected = agent.validate_steps([{"name": "check_on", "args": {"person": "grandma"}}, {"name": "remind", "args": {"person": "Jeanine"}},
                                           {"name": "fly", "args": {}}], validate_command)
    assert kept == [{"name": "check_on", "args": {"person": "Jeanine"}}]
    assert len(rejected) == 2 and rejected[0].startswith("remind: text is required") and rejected[1].startswith("fly:")
    expanded, rejected = agent.validate_steps(care_skills.expand_steps(kept, {}), validate_command)
    assert _names(expanded) == ["find_person", "say", "listen"] and rejected == []


class FakeInference:
    def __init__(self, text):
        self.text, self.calls = text, []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return {"ok": True, "text": self.text, "provider": "local", "model": "fake", "latency_ms": 5}


def test_model_plan_with_care_skills_is_expanded_before_validation():
    inf = FakeInference('{"reply": "I will check on her.", "steps": [{"name": "check_on", "args": {"person": "Grandma"}}, '
                        '{"name": "hydration_reminder", "args": {"person": "Jeanine"}}, {"name": "remind", "args": {"person": "Jeanine"}}]}')
    plan = agent.plan_instruction("check on grandma and get her to drink something", _sit(), inference=inf, validate_command=validate_command)
    assert _names(plan["steps"]) == ["find_person", "say", "listen"] * 2 and plan["source"] == "local:fake"
    assert plan["steps"][0]["args"]["name"] == "Jeanine" and "glass of water" in plan["steps"][4]["args"]["text"]
    assert plan["rejected"] == ["remind: text is required"]
    assert "check_on(person?: string)" in inf.calls[0][0]["content"]  # the planner is told the care skills exist
    plan = agent.plan_instruction("check on grandma", {}, inference=inf)  # no validator: still primitives only
    assert not any(s.get("name") in care_skills.CARE_SKILLS and s.get("args", {}).get("text") for s in plan["steps"])
    assert _names(plan["steps"])[:3] == ["find_person", "say", "listen"]


def test_rule_plan_reaches_the_care_skills():
    plan = agent.rule_plan("check on grandma")
    assert _names(plan["steps"]) == ["find_person", "say", "listen"] and plan["source"] == "rules"
    assert plan["steps"][0]["args"]["name"] == "Jeanine" and plan["steps"][1]["args"]["text"] == "Hi Jeanine, are you alright?"
    ok = agent.rule_plan("Is Grandma ok?")
    assert _names(ok["steps"]) == ["find_person", "say", "listen"] and ok["steps"][0]["args"]["name"] == "Jeanine"
    assert agent.rule_plan("is she okay")["steps"][0]["args"]["name"] is None  # nobody named: the nearest person
    remind = agent.rule_plan("remind Jeanine to take her tablets")
    assert _names(remind["steps"]) == ["find_person", "say", "listen"]
    assert remind["steps"][1]["args"]["text"] == "Hi Jeanine, a gentle reminder: take her tablets."
    assert _names(agent.rule_plan("remind her to drink some water")["steps"]) == ["find_person", "say", "listen"]  # older 'tell' rule
    wave = agent.rule_plan("wave at everyone", _sit())
    assert _names(wave["steps"]) == ["find_person", "hello"] * 3 + ["say"]
    assert _names(agent.rule_plan("wave at Jeanine")["steps"]) == ["find_person", "hello", "say"]  # one person: still a greeting
    for plan in (ok, remind, wave):
        kept, rejected = agent.validate_steps(plan["steps"], validate_command)
        assert rejected == [] and len(kept) == len(plan["steps"])


def test_a_reply_in_conversation_is_not_mistaken_for_a_check_in():
    for heard in ("my hip is not ok", "everything is all right", "that is okay"):  # patrol.py asks rule_plan whether speech is a command
        assert agent.rule_plan(heard)["steps"] == []
