"""Typed stop messages use the same priority/cancellation contract as the Stop button."""
import pytest

from robot.dog.planning.missions import MAX_WAITING, MissionBoard


def instruct(board, text, command_id="typed", **args):
    return board.submit({"command_id": command_id, "name": "instruct",
                         "args": {"text": text, **args}})


@pytest.mark.parametrize("text", ["stop", "pause", "please stop", "stop now", "annie stop",
                                  "  ANNIE, please STOP now!  ", "pause please."])
def test_typed_stop_interrupts_planning_and_full_queue(text):
    board = MissionBoard()
    instruct(board, "walk forward", "planning")
    active = board.take()
    for n in range(MAX_WAITING):
        assert instruct(board, "wave", f"queued-{n}")[0] == 202
    code, receipt = instruct(board, text)
    assert code == 202
    assert receipt["name"] == "stop" and receipt["args"] == {}
    assert receipt["command_id"] == "typed" and receipt["stop_requested"]
    assert active["state"] == "cancelled"
    assert all(board.get(f"queued-{n}")["state"] == "cancelled" for n in range(MAX_WAITING))
    assert board.take() is None
    pending = board.take_stops()
    assert len(pending) == 1
    board.finish_stops(pending, code=0, ack_ms=4)
    assert board.get("typed")["state"] == "completed"
    assert board.take() is None


def test_typed_stop_cancels_parent_and_moving_child_and_discards_remaining_steps():
    board = MissionBoard()
    instruct(board, "walk then wave", "parent")
    parent = board.take()
    board.chain(parent, [{"name": "walk", "args": {"metres": 1}}, {"name": "hello", "args": {}}])
    child = board.take()
    instruct(board, "stop")
    assert parent["state"] == child["state"] == "cancelled"
    assert board.parent is board.current is None
    assert board.queue == []
    board.finish(child, result={"walked_m": 1})
    assert child["state"] == "cancelled"


def test_typed_stop_retry_is_idempotent_and_does_not_cancel_new_work():
    board = MissionBoard()
    code, receipt = instruct(board, "stop")
    assert code == 202
    assert instruct(board, "stop") == (200, receipt)
    pending = board.take_stops()
    assert len(pending) == 1
    board.finish_stops(pending, code=0, ack_ms=1)
    instruct(board, "wave", "new")
    active = board.take()
    assert instruct(board, "stop")[0] == 200
    assert active["state"] == "executing"
    assert not board.stop_requested


@pytest.mark.parametrize("text", ["don't stop", "do not stop", "stop at door", "say stop",
                                  "stop then dance", "please stop at the door", "unstoppable"])
def test_ambiguous_or_compound_phrases_remain_instructions(text):
    board = MissionBoard()
    instruct(board, "walk", "active")
    active = board.take()
    code, receipt = instruct(board, text)
    assert code == 202 and receipt["name"] == "instruct" and receipt["state"] == "queued"
    assert receipt["args"]["text"] == text
    assert active["state"] == "executing" and not board.stop_requested


@pytest.mark.parametrize("args", [{"author": 1}, {"author": ""}, {"text": 42}])
def test_original_instruction_validation_precedes_stop_normalization(args):
    board = MissionBoard()
    payload = {"name": "instruct", "args": {"text": "stop", **args}}
    assert board.submit(payload)[0] == 400
    assert not board.stop_requested and not board.pending_stops


def test_reusing_existing_instruction_id_does_not_turn_it_into_stop():
    board = MissionBoard()
    instruct(board, "wave")
    active = board.take()
    code, receipt = instruct(board, "stop")
    assert code == 200 and receipt["name"] == "instruct"
    assert active["state"] == "executing" and not board.stop_requested
