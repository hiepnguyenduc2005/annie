"""Conversation cannot silently become a movement or an unnamed delivery."""
import json

import pytest

from robot.dog.planning import agent
from robot.dog.runtime.body import validate_command


class Model:
    def __init__(self, steps):
        self.steps = steps
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        return {"ok": True, "provider": "local", "model": "fake", "text": json.dumps({
            "reply": "Done.", "steps": self.steps})}


@pytest.mark.parametrize("text", ["hello Annie", "Annie, hello!", "how are you?", "are you okay?",
                                  "what do you see", "are you paused", "what are you doing"])
def test_conversation_never_acquires_motion_from_model(text):
    model = Model([{"name": "walk", "args": {"metres": 1}}])
    plan = agent.plan_instruction(text, {"mode": "paused"}, inference=model, validate_command=validate_command)
    assert [step["name"] for step in plan["steps"]] == ["say"]
    assert plan["steps"][0]["args"]["text"] == plan["reply"]
    assert not model.calls


def test_scene_reply_uses_current_tracks_not_stale_memory():
    sit = {"people_in_view": [{"name": "Alex"}, {"name": None}], "people": [{"name": "Jeanine"}]}
    reply = agent.rule_plan("what do you see", sit)["reply"]
    assert "Alex" in reply and "haven't identified" in reply and "Jeanine" not in reply
    assert "don't have anyone" in agent.rule_plan("what do you see", {})["reply"]


def test_status_reports_only_supplied_mode():
    assert "paused" in agent.rule_plan("are you paused", {"mode": "paused"})["reply"]
    assert "explore" in agent.rule_plan("what are you doing", {"mode": "explore"})["reply"]
    assert "don't have a current" in agent.rule_plan("are you paused", {})["reply"]


@pytest.mark.parametrize("text", ["say hi to grandma", "say hello to Grandma!", "please say hi to my granny",
                                  "go say hello to nana for me", "tell grandma to charge her phone",
                                  "tell grandma to stop and sit down"])
def test_named_speech_keeps_strict_recipient_even_when_model_omits_find(text):
    plan = agent.plan_instruction(text, {}, inference=Model([{"name": "say", "args": {"text": "hi"}}]),
                                  validate_command=validate_command)
    assert [step["name"] for step in plan["steps"]] == ["find_person", "say", "listen"]
    assert plan["steps"][0]["args"]["name"] == "Jeanine"


@pytest.mark.parametrize("literal", ["hello", "stop and turn around", "check on grandma", "dance"])
def test_say_literal_does_not_execute_quoted_content(literal):
    plan = agent.plan_instruction("say " + literal, {}, validate_command=validate_command)
    assert plan["steps"] == [{"name": "say", "args": {"text": literal}}]


def test_unsupported_motion_is_not_completed_by_empty_model_reply():
    assert agent.plan_instruction("do a backflip", {}, inference=Model([]), validate_command=validate_command)["steps"] == []
    assert agent.rule_plan("do a backflip", {})["steps"] == []


def test_greeting_with_explicit_movement_keeps_command_path():
    plan = agent.rule_plan("turn around and greet the person behind you", {})
    assert [step["name"] for step in plan["steps"]] == ["turn", "find_person", "hello", "say"]


@pytest.mark.parametrize("text", ["don't stop", "do not dance", "Don't turn around", "never walk forward",
                                  "please don't go home", "don’t sit down", "do not stop and dance",
                                  "remember not to patrol", "do not ever move backwards"])
def test_negated_actions_fail_closed_before_model(text):
    model = Model([{"name": "dance", "args": {}}])
    assert agent.rule_plan(text, {})["steps"] == []
    plan = agent.plan_instruction(text, {}, inference=model, validate_command=validate_command)
    assert plan["steps"] == []
    assert "won't carry out" in plan["reply"]
    assert model.calls == 0


@pytest.mark.parametrize("text", ["say don't stop", "say do not dance", "tell grandma not to stop",
                                  "tell grandma do not dance"])
def test_negation_inside_spoken_message_stays_speech(text):
    plan = agent.plan_instruction(text, {}, validate_command=validate_command)
    expected = ["find_person", "say", "listen"] if text.startswith("tell") else ["say"]
    assert [step["name"] for step in plan["steps"]] == expected
    assert any("not" in step["args"].get("text", "") or "don't" in step["args"].get("text", "")
               for step in plan["steps"])


@pytest.mark.parametrize("text", ["walk forward 3 steps", "walk forward three steps", "take 3 steps forward",
                                  "take three steps forward", "walk back 2 steps", "walk backwards two footsteps",
                                  "go forward 5 paces", "move forward a few strides", "walk forward 3 steps and sit",
                                  "turn around and take 3 steps", "walk forward a step", "walk forward eleven steps"])
def test_step_count_motion_is_rejected_without_model(text):
    model = Model([{"name": "walk", "args": {"metres": 1}}])
    plan = agent.plan_instruction(text, {}, inference=model, validate_command=validate_command)
    assert plan["steps"] == []
    assert "cannot count footsteps" in plan["reply"]
    assert "walk forward 1 m" in plan["reply"]
    assert model.calls == 0
    assert agent.rule_plan(text, {})["steps"] == []


@pytest.mark.parametrize("text,expected", [("say walk forward 3 steps", [{"name": "say", "args": {"text": "walk forward 3 steps"}}]),
                                           ("tell grandma to walk forward 3 steps", None)])
def test_step_count_inside_speech_stays_speech(text, expected):
    plan = agent.plan_instruction(text, {}, validate_command=validate_command)
    if expected is not None:
        assert plan["steps"] == expected
    else:
        assert [step["name"] for step in plan["steps"]] == ["find_person", "say", "listen"]
        assert any("3 steps" in step["args"].get("text", "") for step in plan["steps"])


@pytest.mark.parametrize("text,metres", [("walk forward", 1.0), ("walk forward 3 m", 3.0),
                                         ("walk back 1 m", -1.0), ("go ahead 2.5 m", 2.5)])
def test_distance_walking_is_unchanged(text, metres):
    plan = agent.plan_instruction(text, {}, validate_command=validate_command)
    assert plan["steps"] == [{"name": "walk", "args": {"metres": metres}}]
