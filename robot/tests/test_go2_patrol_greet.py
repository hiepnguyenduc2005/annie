"""Pure tests for the patrol-and-greet policy (no hardware)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_patrol_greet import GreetPolicy


def track(tid, h_frac, cx_frac=0.5, fw=640, fh=480):
    h = h_frac * fh
    x = cx_frac * fw
    return {"track_id": tid, "box": [x - 20, 100, x + 20, 100 + h], "conf": 0.8, "posture": "upright", "lying_frames": 0}


def test_patrols_when_nobody_is_visible():
    p = GreetPolicy()
    assert p.step([], 640, 480, now_s=0.0) == ("patrol", None)


def test_new_person_triggers_greet_once_then_followed_for_cooldown():
    p = GreetPolicy(cooldown_s=30)
    assert p.step([track(7, 0.5)], 640, 480, now_s=1.0) == ("greet", 7)
    assert p.step([track(7, 0.5)], 640, 480, now_s=2.0) == ("follow", 7)   # already greeted: keep following
    assert p.step([track(7, 0.5)], 640, 480, now_s=40.0) == ("greet", 7)   # cooldown elapsed


def test_distant_or_off_centre_person_is_followed_not_greeted():
    p = GreetPolicy(min_height_frac=0.45, center_frac=0.18)
    assert p.step([track(3, 0.05)], 640, 480, now_s=0.0) == ("patrol", None)          # too small to matter
    assert p.step([track(3, 0.3)], 640, 480, now_s=0.5) == ("follow", 3)              # visible: follow
    assert p.step([track(3, 0.5, cx_frac=0.2)], 640, 480, now_s=1.0) == ("follow", 3)  # close but off to the side
    assert p.step([track(3, 0.5)], 640, 480, now_s=1.5) == ("greet", 3)               # close and dead ahead


def test_lying_person_is_a_checkin_not_a_greeting():
    p = GreetPolicy()
    t = track(9, 0.4); t["posture"] = "lying"; t["lying_frames"] = 6
    assert p.step([t], 640, 480, now_s=0.0) == ("checkin", 9)


def test_two_people_greets_the_closest_first_then_follows_them():
    p = GreetPolicy()
    assert p.step([track(1, 0.5), track(2, 0.6)], 640, 480, now_s=0.0) == ("greet", 2)
    assert p.step([track(1, 0.5), track(2, 0.6)], 640, 480, now_s=1.0) == ("follow", 2)  # nearest wins, already greeted
    assert p.step([track(1, 0.5)], 640, 480, now_s=2.0) == ("greet", 1)                 # once 2 leaves, 1 is greeted


def test_named_person_gets_a_named_greeting():
    p = GreetPolicy()
    t = track(4, 0.5); t["identity"] = {"name": "Ellis", "score": 0.7}
    assert p.step([t], 640, 480, now_s=0.0) == ("greet", 4)
    assert p.greeting_text(t).startswith("Hi Ellis,") and "feeling" in p.greeting_text(t)  # warm, asks how they are
    assert p.greeting_text(track(5, 0.5)).startswith("Hello there,")
