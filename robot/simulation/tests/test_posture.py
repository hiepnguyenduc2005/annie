# robot/simulation/tests/test_posture.py
from robot.simulation.posture import classify_posture, LYING, UPRIGHT, UNKNOWN
from robot.simulation.person_tracker import PersonTracker

def kp(shoulders, hips, conf=0.9):
    pts = [(0.0, 0.0)] * 17; c = [0.0] * 17
    pts[5], pts[6] = shoulders; pts[11], pts[12] = hips
    for i in (5, 6, 11, 12): c[i] = conf
    return pts, c

def test_vertical_torso_in_tall_box_is_upright():
    pts, c = kp([(100, 50), (140, 50)], [(105, 150), (135, 150)])
    r = classify_posture(pts, c, (90, 30, 150, 260))
    assert r['posture'] == UPRIGHT and r['torso_angle_deg'] < 10

def test_horizontal_torso_in_wide_box_is_lying():
    pts, c = kp([(50, 100), (50, 140)], [(150, 105), (150, 135)])
    r = classify_posture(pts, c, (30, 80, 200, 160))
    assert r['posture'] == LYING and r['torso_angle_deg'] > 80

def test_missing_keypoints_is_unknown():
    pts, c = kp([(100, 50), (140, 50)], [(105, 150), (135, 150)], conf=0.1)
    assert classify_posture(pts, c, (90, 30, 150, 260))['posture'] == UNKNOWN

def test_diagonal_torso_is_unknown_not_guessed():
    pts, c = kp([(100, 100), (120, 100)], [(160, 160), (180, 160)])
    assert classify_posture(pts, c, (90, 90, 200, 180))['posture'] == UNKNOWN

def test_tracker_counts_consecutive_lying_frames_and_drops_stale_tracks():
    frames = iter([
        [{'track_id': 7, 'box': [30, 80, 200, 160], 'conf': 0.8, 'keypoints': kp([(50, 100), (50, 140)], [(150, 105), (150, 135)])[0], 'kp_conf': kp([(0,0)]*2, [(0,0)]*2)[1]}],
        [{'track_id': 7, 'box': [30, 80, 200, 160], 'conf': 0.8, 'keypoints': kp([(50, 100), (50, 140)], [(150, 105), (150, 135)])[0], 'kp_conf': kp([(0,0)]*2, [(0,0)]*2)[1]}],
        [],
    ])
    t = PersonTracker(predictor=lambda jpeg: next(frames))
    a = t.update(b'jpeg', now_ms=0)
    assert a[0]['track_id'] == 7 and a[0]['posture'] == LYING and a[0]['lying_frames'] == 1
    b = t.update(b'jpeg', now_ms=500)
    assert b[0]['lying_frames'] == 2 and b[0]['first_seen_ms'] == 0
    assert t.update(b'jpeg', now_ms=4000) == []
