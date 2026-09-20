# robot/simulation/tests/test_mission.py
import pytest
from robot.simulation.mission import MissionState

WP = [{'id': 'kitchen', 'x': 0.0, 'y': 0.0}, {'id': 'living', 'x': 4.0, 'y': 0.0}, {'id': 'bedroom', 'x': 8.0, 'y': 0.0}]
POSE = lambda x, y, m='house': {'x': x, 'y': y, 'yaw': 0.0, 'map_id': m}

def perception(x, y, person=False, conf=0.9, posture='unknown', location='unknown', ts=1000, frame='f1', map_id='house'):
    return {'person': person, 'posture': posture if person else 'unknown', 'location': location if person else 'unknown',
            'confidence': conf, 'frame_id': frame, 'ts': ts, 'pose': POSE(x, y, map_id), 'caption': 'x'}

def test_new_mission_lists_every_waypoint_unvisited_and_suggests_nearest():
    m = MissionState('house', WP)
    s = m.summary(now_ms=0)
    assert [w['status'] for w in s['waypoints']] == ['unvisited'] * 3
    assert m.next_search_target(POSE(3.5, 0), now_ms=0) == 'living'
    assert s['last_sighting'] is None and s['destination'] is None

def test_completed_goto_marks_visited_and_empty_view_marks_inspected_empty():
    m = MissionState('house', WP)
    assert m.command_update({'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'executing', 'command_id': 'c1'}, now_ms=100) == []
    assert m.summary(now_ms=100)['destination'] == {'waypoint_id': 'kitchen', 'command_id': 'c1'}
    assert m.command_update({'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'completed', 'command_id': 'c1'}, now_ms=200) == ['arrived']
    assert m.summary(now_ms=200)['destination'] is None
    assert m.observe(perception(0.2, 0.1, ts=300), now_ms=300) == ['inspected_empty']
    assert m.summary(now_ms=300)['waypoints'][0]['status'] == 'inspected_empty'
    assert m.next_search_target(POSE(0, 0), now_ms=300) == 'living'

def test_person_seen_records_sighting_and_replans():
    m = MissionState('house', WP)
    events = m.observe(perception(4.1, 0.2, person=True, posture='lying', location='floor', ts=500, frame='f9'), now_ms=500)
    assert events == ['person_seen']
    s = m.summary(now_ms=500)
    assert s['waypoints'][1]['status'] == 'person_seen'
    assert s['last_sighting']['waypoint_id'] == 'living' and s['last_sighting']['frame_id'] == 'f9'
    assert m.should_replan(now_ms=500, events=events, last_plan_ms=490)

def test_low_confidence_person_is_not_a_sighting():
    m = MissionState('house', WP)
    assert m.observe(perception(4.0, 0.0, person=True, conf=0.3, ts=1), now_ms=1) == []
    assert m.summary(now_ms=1)['last_sighting'] is None

def test_inspected_empty_expires_and_becomes_searchable_again():
    m = MissionState('house', WP, inspected_ttl_ms=1000)
    for wp in WP:
        m.observe(perception(wp['x'], wp['y'], ts=10), now_ms=10)
    assert m.next_search_target(POSE(0, 0), now_ms=10) is None
    assert m.next_search_target(POSE(0, 0), now_ms=2000) == 'kitchen'
    assert m.summary(now_ms=2000)['waypoints'][0]['status'] == 'visited'

def test_failed_goto_clears_destination_and_reports():
    m = MissionState('house', WP)
    m.command_update({'cmd': 'goto', 'waypoint': 'bedroom', 'status': 'accepted', 'command_id': 'c2'}, now_ms=1)
    assert m.command_update({'cmd': 'goto', 'waypoint': 'bedroom', 'status': 'failed', 'command_id': 'c2'}, now_ms=2) == ['command_failed']
    assert m.summary(now_ms=2)['destination'] is None
    assert m.summary(now_ms=2)['waypoints'][2]['status'] == 'unvisited'

def test_other_map_perception_is_ignored():
    m = MissionState('house', WP)
    assert m.observe(perception(0, 0, person=True, map_id='garage', ts=5), now_ms=5) == []
    assert m.summary(now_ms=5)['last_sighting'] is None

def test_far_from_any_waypoint_changes_nothing():
    m = MissionState('house', WP, arrive_radius_m=1.0)
    assert m.observe(perception(2.0, 3.0, ts=5), now_ms=5) == []
    assert all(w['status'] == 'unvisited' for w in m.summary(now_ms=5)['waypoints'])

@pytest.mark.parametrize('last,now,events,expected', [(None, 0, [], True), (0, 1000, [], False), (0, 30000, [], True), (0, 1, ['arrived'], True)])
def test_should_replan_rules(last, now, events, expected):
    assert MissionState('house', WP).should_replan(now_ms=now, events=events, last_plan_ms=last) is expected
