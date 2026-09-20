"""Pure tests for the follow controller (no hardware)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_follow import follow_command


def test_person_far_and_centered_walks_forward_straight():
    vx, wz, why = follow_command((300, 100, 340, 200), 640, 480)  # small box, centered
    assert why == "ok" and vx > 0.2 and abs(wz) < 0.05


def test_person_on_the_left_turns_left_before_walking():
    vx, wz, why = follow_command((20, 100, 60, 200), 640, 480)
    assert why == "ok" and wz > 0.5 and vx == 0.0


def test_person_on_the_right_turns_right():
    vx, wz, why = follow_command((580, 100, 620, 200), 640, 480)
    assert wz < -0.5


def test_person_at_target_distance_holds():
    h = int(0.55 * 480)
    vx, wz, why = follow_command((300, 10, 340, 10 + h), 640, 480)
    assert why == "ok" and vx == 0.0


def test_person_too_close_stops():
    vx, wz, why = follow_command((200, 0, 440, 470), 640, 480)
    assert why == "too_close" and (vx, wz) == (0.0, 0.0)


def test_speed_is_capped():
    vx, wz, why = follow_command((318, 200, 322, 210), 640, 480, max_vx=0.3)
    assert vx <= 0.3
