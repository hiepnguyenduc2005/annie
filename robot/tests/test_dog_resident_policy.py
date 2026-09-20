"""ResidentPolicy regressions: check-in dedupe survives tracker id churn without hiding a second resident.

Pure unit tests: no MuJoCo, no tracker, no hardware. The scenario behind every test is
story B of the Monte-Carlo harness: the dog asks a lying resident, drives away, the
tracker loses them for over two seconds (ByteTrack expiry), then sees them lying again
on a new track id -- and must not ask twice. Symmetrically, a genuinely different
resident down elsewhere must always get their own check-in.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robot.dog.planning.resident_policy import MAX_EPISODES, ResidentPolicy, norm_name  # noqa: E402


def lying(tid, name=None, *, frames=8):
    track = {"track_id": tid, "posture": "lying", "lying_frames": frames}
    if name:
        track["identity"] = {"name": name, "score": 0.8, "method": "shirt_colour"}
    return track


def test_no_suppression_before_any_ask():
    policy = ResidentPolicy()
    assert policy.should_ask(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)


def test_same_track_id_is_asked_once_within_the_window():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    assert not policy.should_ask(lying(1, "Jeanine"), (1.0, 2.0), now_s=15.0)


def test_new_track_id_same_name_is_the_same_resident():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    assert not policy.should_ask(lying(2, "Jeanine"), (1.1, 2.2), now_s=30.0)


def test_new_track_id_unnamed_same_place_is_suppressed():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), (1.0, 2.0), now_s=10.0, radius_m=1.0)
    assert not policy.should_ask(lying(2), (1.4, 2.3), now_s=30.0, radius_m=1.0)


def test_unnamed_person_down_elsewhere_is_a_new_check_in():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), (1.0, 2.0), now_s=10.0, radius_m=1.0)
    assert policy.should_ask(lying(2), (5.0, -3.0), now_s=30.0, radius_m=1.0)


def test_two_named_residents_down_are_two_check_ins():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    assert policy.should_ask(lying(2, "Walter"), (1.2, 2.1), now_s=12.0)


def test_unknown_identity_with_no_geometry_never_suppresses():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), None, now_s=10.0)
    assert policy.should_ask(lying(2), None, now_s=12.0)


def test_upright_sighting_of_the_same_name_resolves_the_episode():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    policy.observe([{"track_id": 3, "posture": "upright", "identity": {"name": "Jeanine"}}], {3: (2.0, 3.0)}, now_s=40.0)
    assert policy.should_ask(lying(4, "Jeanine"), (1.0, 2.0), now_s=50.0), "a new fall after verified recovery must ask again"


def test_upright_sighting_near_the_place_resolves_an_unnamed_episode():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), (1.0, 2.0), now_s=10.0, radius_m=1.0)
    policy.observe([{"track_id": 3, "posture": "upright"}], {3: (1.5, 2.4)}, now_s=40.0)
    assert policy.should_ask(lying(2), (1.0, 2.0), now_s=50.0)


def test_someone_else_getting_up_does_not_resolve_the_episode():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    policy.observe([{"track_id": 3, "posture": "upright", "identity": {"name": "Walter"}}], {3: (1.1, 2.1)}, now_s=40.0)
    assert not policy.should_ask(lying(2, "Jeanine"), (1.0, 2.0), now_s=50.0)


def test_unknown_posture_does_not_resolve_the_episode():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    policy.observe([{"track_id": 3, "posture": "unknown", "identity": {"name": "Jeanine"}}], {3: (2.0, 3.0)}, now_s=40.0)
    assert not policy.should_ask(lying(2, "Jeanine"), (1.0, 2.0), now_s=50.0), "a missed pose estimate is not recovery"


def test_window_expiry_asks_again():
    policy = ResidentPolicy(episode_window_s=60.0)
    policy.mark_asked(lying(1, "Jeanine"), (1.0, 2.0), now_s=10.0)
    assert policy.should_ask(lying(2, "Jeanine"), (1.0, 2.0), now_s=200.0), "after a long quiet, asking again is the safe answer"


def test_memory_stays_bounded():
    policy = ResidentPolicy()
    for k in range(MAX_EPISODES + 10):
        policy.mark_asked(lying(k), (float(k), 0.0), now_s=k)
    assert len(policy._asked) <= MAX_EPISODES


def test_norm_name_normalises_and_rejects_blanks():
    assert norm_name({"identity": {"name": " Jeanine "}}) == "jeanine"
    assert norm_name({"identity": {"name": ""}}) is None
    assert norm_name({"identity": None}) is None
    assert norm_name({}) is None


def test_upright_person_a_little_further_off_does_not_resolve_an_unnamed_episode():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), (1.0, 2.0), now_s=10.0, radius_m=1.0)
    policy.observe([{"track_id": 3, "posture": "upright"}], {3: (2.4, 3.0)}, now_s=40.0)
    assert not policy.should_ask(lying(2), (1.0, 2.0), now_s=50.0), "someone up 1.7 m away is not proof this resident recovered"


def test_guest_alias_is_never_a_stable_identity():
    assert norm_name(lying(1, "Guest 1")) is None
    assert norm_name({"track_id": 1, "posture": "lying", "identity": {"name": "Guest 2", "method": "guest"}}) is None
    policy = ResidentPolicy()
    policy.mark_asked({"track_id": 1, "posture": "lying", "identity": {"name": "Guest 1", "method": "guest"}},
                      (1.0, 2.0), now_s=10.0)
    # Guests are clothing + place only: place is the only dedupe evidence, so the same
    # place is one resident (asking again adds nothing), but a "Guest 1" alias seen down
    # somewhere else is never assumed to be the same person.
    assert not policy.should_ask({"track_id": 2, "posture": "lying", "identity": {"name": "Guest 1", "method": "guest"}},
                                 (1.0, 2.0), now_s=12.0), "same place: spatial evidence still dedupes"
    assert policy.should_ask({"track_id": 3, "posture": "lying", "identity": {"name": "Guest 1", "method": "guest"}},
                             (5.0, -3.0), now_s=14.0), "an alias elsewhere is not evidence of one resident"


def test_two_simultaneous_unnamed_lying_tracks_close_together_are_two_check_ins():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), (1.0, 2.0), now_s=10.0, radius_m=1.0)
    assert policy.should_ask(lying(2), (1.5, 2.3), now_s=12.0, radius_m=1.0,
                             visible_lying_tids={1, 2}), "two people visibly down together are two residents"


def test_churn_still_suppresses_when_the_original_track_is_gone():
    policy = ResidentPolicy()
    policy.mark_asked(lying(1), (1.0, 2.0), now_s=10.0, radius_m=1.0)
    assert not policy.should_ask(lying(2), (1.4, 2.3), now_s=30.0, radius_m=1.0,
                                 visible_lying_tids={2}), "one resident re-seen on a new id is still one episode"
