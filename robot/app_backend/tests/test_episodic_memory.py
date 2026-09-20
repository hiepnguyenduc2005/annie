import json
import sqlite3
from uuid import uuid4

from robot.app_backend.app.episodic_memory import EpisodicMemory

BASE = 1_700_000_000_000
DAY = 86_400_000
NOW = BASE + 3_700_000
WAYPOINTS = [{'id': 'living-room', 'x': 2, 'y': 2, 'label': 'Living room'},
             {'id': 'bedroom', 'x': 6, 'y': 2, 'label': 'Bedroom'}]
SERVICE_WAYPOINTS = [{'id': waypoint['id'], 'x': waypoint['x'], 'y': waypoint['y']}
                     for waypoint in WAYPOINTS]


def frame(fid, ts, caption, x=0.0, y=0.0, map_id='demo-home'):
    return {'schema_version': '0.1', 'source': 'simulation_vlm', 'model': 'demo-vlm', 'ts': ts,
            'frame_id': str(fid), 'person': True, 'posture': 'sitting', 'location': 'bed',
            'confidence': .9, 'caption': caption, 'pose': {'x': x, 'y': y, 'yaw': 0., 'map_id': map_id}}


def make_db(frames):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE memory (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
    for item in frames:
        db.execute('INSERT INTO memory VALUES (?, ?, ?)', (item['frame_id'], item['ts'], json.dumps(item)))
    return db


def sample_frames():
    return [
        frame(uuid4(), BASE, 'resident resting on the bed; glasses on the bedside table', x=6, y=2),
        frame(uuid4(), BASE + 3_600_000, 'blue mug on the coffee table', x=2, y=2),
        frame(uuid4(), BASE - 90_000, 'blue mug on the kitchen counter', map_id='demo-home-old'),
    ]


def memory(frames):
    return EpisodicMemory(make_db(frames), now=lambda: NOW, map_id='demo-home', waypoints=WAYPOINTS)


def test_where_cites_actual_frame_and_pose():
    frames = sample_frames()
    result = memory(frames).ask('where was the blue mug')
    assert result['answerable'] and result['kind'] == 'where'
    assert result['citations'][0]['frame_id'] == frames[1]['frame_id']
    assert result['citations'][0]['ts'] == BASE + 3_600_000
    assert result['citations'][0]['pose']['x'] == 2
    assert result['citations'][0]['map_current'] is True
    assert result['citations'][0]['source'] == 'simulation_vlm'
    assert 'x=2' in result['answer']


def test_old_map_observations_are_labeled_not_current():
    frames = sample_frames()
    result = memory(frames).ask('where was the counter mug')
    assert result['answerable']
    assert result['citations'][0]['frame_id'] == frames[2]['frame_id']
    assert result['citations'][0]['map_current'] is False
    assert 'not the current map' in result['answer']


def test_last_seen_reports_latest_match_with_time():
    frames = sample_frames()
    result = memory(frames).ask('when did I last see the blue mug')
    assert result['kind'] == 'last_seen'
    assert result['citations'][0]['ts'] == BASE + 3_600_000
    assert '2023-11-14' in result['answer']


def test_person_last_seen_ignores_newer_negative_capture():
    positive = frame('positive', BASE, 'A person stands in the hallway', x=2, y=2)
    negative = {**frame('negative', BASE + 1000, 'No person visible', x=6, y=2),
                'person': False}
    result = memory([positive, negative]).ask('where was the person last seen')
    assert result['answerable'] and result['kind'] == 'last_seen'
    assert [c['frame_id'] for c in result['citations']] == ['positive']
    assert result['citations'][0]['ts'] == positive['ts']
    assert result['citations'][0]['pose'] == positive['pose']
    assert 'No person visible' not in result['answer']


def test_person_last_seen_refuses_when_only_negative_captures_exist():
    negative = {**frame('negative', BASE, 'No person visible'), 'person': False}
    result = memory([negative]).ask('where was the person last seen')
    assert not result['answerable']
    assert result['kind'] == 'no_evidence'
    assert result['citations'] == []


def test_person_last_seen_uses_positive_flag_without_literal_person_caption():
    positive = frame('human', BASE, 'A human stands nearby')
    result = memory([positive]).ask('where was the person last seen')
    assert result['answerable']
    assert [c['frame_id'] for c in result['citations']] == ['human']


def test_person_query_retains_waypoint_and_caption_constraints():
    frames = [
        frame('bedroom-standing', BASE, 'A human standing beside the bed', x=6, y=2),
        frame('bedroom-sitting', BASE + 1000, 'A human sitting on the bed', x=6, y=2),
        frame('living-standing', BASE + 2000, 'A human standing nearby', x=2, y=2),
        frame('old-map-standing', BASE + 3000, 'A human standing nearby',
              x=6, y=2, map_id='demo-home-old'),
    ]
    result = memory(frames).ask('where was the standing person last seen near the bedroom')
    assert result['answerable']
    assert [c['frame_id'] for c in result['citations']] == ['bedroom-standing']


def test_person_query_retains_time_window():
    result = memory([frame('old-human', BASE, 'A human stands nearby')]).ask(
        'person in the last 30 minutes')
    assert not result['answerable']
    assert result['citations'] == []


def test_object_query_still_retrieves_frames_without_people():
    negative = {**frame('mug', BASE, 'A blue mug on the table; no person visible'),
                'person': False}
    result = memory([negative]).ask('where was the blue mug')
    assert result['answerable']
    assert [c['frame_id'] for c in result['citations']] == ['mug']


def test_duration_spans_first_to_last_observation():
    frames = [frame(uuid4(), BASE, 'glasses on the bedside table'),
              frame(uuid4(), BASE + 600_000, 'glasses on the bedside table')]
    result = memory(frames).ask('how long were the glasses visible')
    assert result['kind'] == 'duration'
    assert '10 min' in result['answer']
    assert {c['ts'] for c in result['citations']} == {BASE, BASE + 600_000}


def test_duration_with_single_capture_refuses():
    frames = [frame(uuid4(), BASE, 'glasses on the bedside table')]
    result = memory(frames).ask('how long were the glasses visible')
    assert not result['answerable'] and result['kind'] == 'duration_unknown'
    assert 'continuous' not in result['answer'].lower()
    assert result['citations'][0]['ts'] == BASE


def test_when_prefers_latest_capture_over_higher_score():
    frames = [
        frame(uuid4(), BASE + 1000, 'blue mug on the coffee table'),
        frame(uuid4(), BASE, 'blue mug on the coffee table next to the keys'),
    ]
    result = memory(frames).ask('when did I last see the blue mug')
    assert result['citations'][0]['ts'] == BASE + 1000


def test_partial_entity_overlap_is_not_a_false_hit():
    frames = [frame(uuid4(), BASE, 'blue cup on the table')]
    result = memory(frames).ask('where is the blue umbrella')
    assert not result['answerable']
    assert result['citations'] == []


def test_plural_query_matches_singular_caption():
    frames = [frame(uuid4(), BASE, 'blue cup on the table')]
    result = memory(frames).ask('where are the blue cups')
    assert result['answerable']


def test_bare_number_and_time_tokens_do_not_require_caption_match():
    frames = [frame(uuid4(), BASE + 1000, 'blue mug on the table')]
    result = memory(frames).ask('blue mug 3 minutes ago')
    assert result['answerable']


def test_near_waypoint_ignores_coordinates_from_other_maps():
    frames = [frame(uuid4(), BASE, 'blue mug on the table', x=6, y=2, map_id='demo-home-old')]
    result = memory(frames).ask('blue mug near the bedroom')
    assert not result['answerable']
    assert result['kind'] == 'not_near'


def test_future_frames_are_excluded_not_cited():
    frames = sample_frames()
    future = frame(uuid4(), BASE + 99_000_000, 'blue mug on the moon')
    result = memory(frames + [future]).ask('blue mug')
    assert result['excluded_future'] == 1
    assert all(c['ts'] <= NOW for c in result['citations'])


def test_old_memories_stay_valid_but_are_labeled_historical():
    frames = [frame(uuid4(), BASE - 2 * DAY, 'blue mug on the shelf')]
    result = memory(frames).ask('blue mug')
    assert result['answerable']
    assert result['citations'][0]['historical'] is True


def test_unknown_entity_refuses_without_evidence():
    result = memory(sample_frames()).ask('where was the elephant')
    assert not result['answerable']
    assert result['citations'] == []
    assert result['answer'] == 'I wasn\u2019t there for that'


def test_identity_questions_are_refused():
    result = memory(sample_frames()).ask('who was in the bedroom')
    assert not result['answerable']
    assert result['kind'] == 'identity_refused'


def test_time_window_restricts_matches():
    frames = sample_frames()
    result = memory(frames).ask('blue mug in the last 30 minutes')
    assert result['answerable']
    assert [c['ts'] for c in result['citations']] == [BASE + 3_600_000]


def test_waypoint_proximity_distinguishes_rooms():
    frames = sample_frames()
    result = memory(frames).ask('what did you see near the bedroom')
    assert result['answerable']
    assert result['citations'][0]['frame_id'] == frames[0]['frame_id']


def test_adapter_reads_service_database():
    from robot.app_backend.app.service import Service, now_ms
    service = Service(':memory:')
    service.ingest('dog.map', {'ts': now_ms(), 'map_id': 'demo-home', 'origin': {'x': 0, 'y': 0},
                               'resolution_m': .05, 'waypoints': SERVICE_WAYPOINTS, 'rooms': []})
    fid = uuid4()
    service.ingest('brain.perception', {'ts': now_ms(), 'frame_id': str(fid), 'person': True,
                                        'posture': 'sitting', 'location': 'bed', 'confidence': .9,
                                        'caption': 'glasses on the bedside table',
                                        'pose': {'x': 6, 'y': 2}})
    result = EpisodicMemory(service.db, now=service.clock).ask('glasses')
    assert result['answerable']
    assert result['citations'][0]['frame_id'] == str(fid)
    service.close()
