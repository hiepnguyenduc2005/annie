"""Current-goal delivery receipts; no services, audio players, or inference."""
import pytest

from robot.simulation.goal_completion import IssuedSpeech, goal_speech_completion


def check(ids, commands, clips=(), **kwargs):
    return goal_speech_completion(
        [IssuedSpeech(cid, "home", 7) for cid in ids], commands, clips,
        map_id="home", goal_revision=7, **kwargs)


def receipt(cid="current", status="completed", cmd="say"):
    return {"command_id": cid, "status": status, "cmd": cmd}


def clip(cid="current", status="played"):
    return {"command_id": cid, "status": status}


def test_failed_current_goal_speech_cannot_count_as_completion():
    result = check(
        ["current"], [receipt(status="failed")], [clip(status="failed")])
    assert result.state == "failed"
    assert result.failed_ids == ("current",)
    assert not result.allows_completion
    assert not result.delivery_verified


@pytest.mark.parametrize("status", ["queued", "accepted", "executing", None, "unknown"])
def test_unfinished_or_unknown_app_receipt_cannot_finish_delivery(status):
    result = check(["current"], [receipt(status=status)])
    assert result.state == "pending"
    assert result.pending_ids == ("current",)
    assert not result.allows_completion


@pytest.mark.parametrize("status", ["queued", "generated", "playing", None, "completed"])
def test_synthesis_or_in_progress_viewer_clip_is_not_played(status):
    result = check(["current"], [receipt()], [clip(status=status)])
    assert result.state == "pending"
    assert not result.delivery_verified


def test_failed_viewer_clip_overrides_conflicting_completed_app_snapshot():
    result = check(["current"], [receipt()], [clip(status="failed")])
    assert result.state == "failed"


def test_completed_playback_allows_completion_with_identified_receipts():
    result = check(["current"], [receipt()], [clip()])
    assert result.state == "completed"
    assert result.allows_completion and result.delivery_verified


def test_evicted_viewer_clip_does_not_discard_completed_app_receipt():
    result = check(["current"], [receipt()])
    assert result.delivery_verified


def test_prior_goal_and_other_map_failures_do_not_block_current_delivery():
    commands = [receipt("previous", "failed"), receipt("old-map", "executing"), receipt()]
    clips = [clip("previous", "failed"), clip("old-map", "playing"), clip()]
    result = check(["current"], commands, clips)
    assert result.state == "completed"
    assert not result.failed_ids and not result.pending_ids


def test_other_goal_success_cannot_substitute_for_missing_current_receipt():
    result = check(["current"], [receipt("previous")], [clip("previous")])
    assert result.state == "pending"
    assert result.pending_ids == ("current",)


def test_motion_completion_cannot_substitute_for_speech_playback():
    result = check(["current"], [receipt(cmd="goto")], [clip()])
    assert result.state == "pending"


def test_every_required_current_goal_clip_must_complete():
    result = check(
        ["intro", "message", "question"],
        [receipt("intro"), receipt("message", "failed"), receipt("question", "executing")])
    assert result.state == "failed"
    assert result.failed_ids == ("message",)
    assert result.pending_ids == ("question",)


def test_later_clip_does_not_silently_supersede_an_undelivered_message():
    result = check(
        ["message", "later"], [receipt("message", "failed"), receipt("later")])
    assert not result.allows_completion


def test_duplicate_required_ids_are_checked_once():
    result = check(["current", "current"], [])
    assert result.pending_ids == ("current",)


@pytest.mark.parametrize("earlier", ["failed", "executing"])
def test_conflicting_duplicate_receipt_cannot_be_hidden_by_last_success(earlier):
    result = check(["current"], [receipt(status=earlier), receipt()])
    assert not result.allows_completion


def test_observation_goal_without_speech_may_finish_but_proves_no_delivery():
    result = check([], [receipt("previous", "failed")])
    assert result.state == "not_required" and result.allows_completion
    assert not result.delivery_verified


def test_delivery_goal_without_any_issued_speech_cannot_finish():
    result = check([], [receipt("previous")], require_speech=True)
    assert result.state == "pending"
    assert not result.allows_completion and not result.delivery_verified


@pytest.mark.parametrize("cid", ["", " ", None, 7])
def test_invalid_goal_owned_id_is_rejected(cid):
    with pytest.raises(ValueError, match="nonempty strings"):
        check([cid], [])


def test_inputs_are_not_mutated():
    ids, commands, clips = ["current"], [receipt()], [clip()]
    check(ids, commands, clips)
    assert ids == ["current"] and commands == [receipt()] and clips == [clip()]


def test_issued_records_are_scoped_by_both_map_and_goal_revision():
    issued = [IssuedSpeech("old-goal", "home", 6),
              IssuedSpeech("old-map", "previous-home", 7),
              IssuedSpeech("current", "home", 7)]
    result = goal_speech_completion(
        issued, [receipt("old-goal", "failed"), receipt("old-map", "failed"), receipt()],
        [clip("old-goal", "failed"), clip("old-map", "failed"), clip()],
        map_id="home", goal_revision=7, require_speech=True)
    assert result.delivery_verified
    assert not result.failed_ids


def test_prior_revision_playback_cannot_satisfy_new_delivery_goal():
    result = goal_speech_completion(
        [IssuedSpeech("previous", "home", 6)], [receipt("previous")], [clip("previous")],
        map_id="home", goal_revision=7, require_speech=True)
    assert result.state == "pending"
    assert not result.delivery_verified


def test_goal_revision_reused_after_map_change_does_not_reuse_playback():
    result = goal_speech_completion(
        [IssuedSpeech("previous", "old-home", 7)], [receipt("previous")],
        map_id="home", goal_revision=7, require_speech=True)
    assert result.state == "pending"


@pytest.mark.parametrize("revision", [None, -1, True, "7"])
def test_invalid_revision_rejects_ambiguous_goal_ownership(revision):
    with pytest.raises(ValueError, match="integer revision"):
        goal_speech_completion([], [], map_id="home", goal_revision=revision)


@pytest.mark.parametrize("map_id", [None, "", " "])
def test_missing_map_rejects_ambiguous_goal_ownership(map_id):
    with pytest.raises(ValueError, match="map ID"):
        goal_speech_completion([], [], map_id=map_id, goal_revision=7)
