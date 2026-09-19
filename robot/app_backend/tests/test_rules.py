from uuid import uuid4
import pytest
from robot.app_backend.app.service import Service


@pytest.fixture
def service():
    now = [100000]
    svc = Service(':memory:', clock=lambda: now[0])
    svc.time = now
    yield svc
    svc.close()


def observe(svc, **changes):
    svc.time[0] += 1
    frame = dict(ts=svc.time[0], frame_id=str(uuid4()), person=True, posture='lying', location='floor', confidence=.95, caption='Resident beside blue mug', pose={'x': 1, 'y': 2})
    frame.update(changes)
    return svc.ingest('brain.perception', frame), frame


def suspect(svc):
    observe(svc)
    observe(svc)
    assert svc.pending
    return svc.pending['event_id']


@pytest.mark.parametrize('changes', [{'location': 'bed'}, {'location': 'unknown'}, {'posture': 'unknown'}, {'confidence': .79}, {'person': False}, {'ts': 1}, {'ts': 200000}])
def test_no_alert_for_safe_or_invalid_evidence(service, changes):
    observe(service, **changes)
    observe(service, **changes)
    assert service.events() == []


def test_distinct_frames_order_and_timeout_once(service):
    _, frame = observe(service)
    service.time[0] += 1
    assert not service.ingest('brain.perception', {**frame, 'ts': service.time[0]})
    assert service.events() == []
    observe(service)
    assert len(service.events()) == 1
    deadline = service.pending['deadline_at']
    service.tick(deadline - 1)
    assert len(service.events()) == 1
    service.tick(deadline)
    service.tick(deadline + 1)
    assert [event['kind'] for event in service.events()].count('fall_confirmed') == 1
    observe(service)
    observe(service)
    assert len(service.events()) == 3
    # A resolved episode recovers only through two confident, present,
    # affirmatively safe captures at least 500 ms apart within 5 s.
    observe(service, location='bed')
    service.time[0] += 600
    observe(service, location='bed')
    suspect(service)
    assert len(service.events()) == 4


def test_gap_breaks_consecutive_observations(service):
    observe(service)
    service.time[0] += 6000
    observe(service)
    assert service.events() == []


@pytest.mark.parametrize('text, confidence, correlated, expected', [('okay', .99, True, 'checkin_ok'), ('help', .99, True, 'fall_confirmed'), ('maybe', .99, True, None), ('not okay', .99, True, None), ('okay', .5, True, None), ('okay', .99, False, None)])
def test_replies(service, text, confidence, correlated, expected):
    event_id = suspect(service)
    service.ingest('voice.heard', {'ts': service.clock(), 'text': text, 'confidence': confidence, 'event_id': event_id if correlated else str(uuid4())})
    kinds = [event['kind'] for event in service.events()]
    assert (expected in kinds) if expected else kinds == ['fall_suspected']
    assert (service.pending is None) == bool(expected)


def test_late_reply_cannot_cancel(service):
    event_id = suspect(service)
    service.time[0] += 8000
    service.ingest('voice.heard', {'ts': service.clock(), 'text': 'okay', 'confidence': 1, 'event_id': event_id})
    assert 'fall_confirmed' in [event['kind'] for event in service.events()]


def test_ack_and_memory_survive_restart(tmp_path):
    db = str(tmp_path / 'test.sqlite')
    svc = Service(db, clock=lambda: 100000)
    svc.ingest('brain.perception', {'ts': 99999, 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying', 'location': 'floor', 'confidence': .95, 'caption': 'Blue mug on table', 'pose': {'x': 2, 'y': 1}})
    svc.ingest('brain.perception', {'ts': 100000, 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying', 'location': 'floor', 'confidence': .95, 'caption': 'Glasses beside mug', 'pose': {'x': 2, 'y': 1}})
    event = svc.events()[0]
    ack = svc.ack(event['event_id'], 'family')
    assert svc.pending is not None
    assert svc.ack(event['event_id'], 'family') == ack
    svc.close()
    restored = Service(db)
    assert restored.events()[0] == ack
    assert restored.query('Where is my mug?')['answerable']
    assert not restored.query('purple elephant')['answerable']
    assert len(restored.query('mug')['citations']) == 2
    restored.close()


def test_unknown_resets_candidate(service):
    observe(service)
    observe(service, location='unknown')
    observe(service)
    assert not service.events()


def test_nonfinite_coordinates_rejected():
    from robot.app_backend.app.models import Pose
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Pose(x=float('inf'), y=0)


def test_low_confidence_breaks_pair_but_does_not_rearm_episode(service):
    observe(service)
    observe(service, confidence=.2)
    observe(service)
    assert service.events() == []
    observe(service)
    service.tick(service.pending['deadline_at'])
    observe(service, confidence=.2)
    observe(service)
    observe(service)
    assert [event['kind'] for event in service.events()].count('fall_suspected') == 1


def test_frozen_clock_demo_sequence_and_no_future_events():
    svc = Service(':memory:', clock=lambda: 100000)
    try:
        svc.seed()
        svc.scenario('fall')
        first = svc.pending['event_id']
        svc.scenario('fall')  # Repeated button cannot duplicate an ongoing episode.
        assert len(svc.events()) == 1
        svc.scenario('okay')
        assert svc.pending is None
        svc.scenario('safe_bed')
        svc.scenario('fall')
        assert svc.pending['event_id'] != first
        svc.scenario('timeout')
        svc.scenario('timeout')
        assert svc.pending is None
        kinds = [event['kind'] for event in svc.events()]
        assert kinds.count('fall_suspected') == 2
        assert kinds.count('checkin_ok') == 1
        assert kinds.count('checkin_no_reply') == 1
        assert kinds.count('fall_confirmed') == 1
        assert {event['ts'] for event in svc.events()} == {100000}
        assert svc.db.execute('SELECT COUNT(*) FROM memory').fetchone()[0] == 8
    finally:
        svc.close()


def test_equal_timestamp_distinct_frames_allowed_but_replay_and_older_rejected(service):
    _, frame = observe(service)
    assert not service.ingest('brain.perception', frame)
    assert not service.ingest('brain.perception', {**frame, 'frame_id': str(uuid4()), 'ts': frame['ts'] - 1})
    assert service.ingest('brain.perception', {**frame, 'frame_id': str(uuid4())})
    assert service.pending


def test_map_change_breaks_candidate_without_map_announcement(service):
    observe(service, pose={'x': 1, 'y': 1, 'map_id': 'A'})
    observe(service, pose={'x': 1, 'y': 1, 'map_id': 'B'})
    assert service.pending is None
    observe(service, pose={'x': 1, 'y': 1, 'map_id': 'B'})
    assert service.pending


def test_map_reset_rejects_mismatch_and_preserves_pending_evidence(service):
    observe(service, pose={'x': 1, 'y': 1, 'map_id': 'A'})
    service.ingest('dog.map', {'ts': service.clock(), 'map_id': 'B', 'origin': {'x': 0, 'y': 0}, 'resolution_m': .05, 'waypoints': []})
    accepted, _ = observe(service, pose={'x': 1, 'y': 1, 'map_id': 'A'})
    assert not accepted
    observe(service, pose={'x': 1, 'y': 1, 'map_id': 'B'})
    assert service.pending is None
    observe(service, pose={'x': 1, 'y': 1, 'map_id': 'B'})
    pending = service.pending.copy()
    evidence = service.pending_evidence.copy()
    service.ingest('dog.map', {'ts': service.clock(), 'map_id': 'C', 'origin': {'x': 0, 'y': 0}, 'resolution_m': .05, 'waypoints': []})
    assert service.pending == pending
    assert service.pending_evidence == evidence
    assert evidence['pose']['map_id'] == 'B'


def test_demo_timeout_does_not_hide_later_wall_clock_events(service):
    service.scenario('fall')
    service.scenario('timeout')
    cursor = max(event['ts'] for event in service.events())
    assert cursor == service.clock()
    service.time[0] += 1
    service.scenario('safe_bed')
    service.scenario('fall')
    assert [event['kind'] for event in service.events(cursor)] == ['fall_suspected']


def test_slow_subscriber_receives_bounded_resync_notice(service):
    import asyncio
    queue = asyncio.Queue(maxsize=100)
    service.subscribers.add(queue)
    service.scenario('fall')
    for _ in range(400):
        service.queue_command({'cmd': 'look'})
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    assert len(messages) <= 100
    assert messages[0] == {'type': 'resync', 'data': {'reason': 'subscriber_overflow'}}
    assert service.status()['pending_checkin']
    assert len(service.events()) == 1
    assert len(service.commands) == 100
