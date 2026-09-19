"""Audio-delivery policy: only a completed simulation receipt for the check-in
say command counts as delivery and opens the eight-second reply window."""
from collections import Counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from robot.app_backend.app.main import create_app
from robot.app_backend.app.service import Service


@pytest.fixture
def service():
    now = [100000]
    svc = Service(':memory:', clock=lambda: now[0], require_audio_receipt=True)
    svc.time = now
    yield svc
    svc.close()


def observe(svc, **changes):
    # Receipt mode follows the acceptance profile: qualifying captures are at
    # least 250 ms apart; the legacy demo keeps same-millisecond pairs.
    svc.time[0] += 250 if svc.require_audio_receipt else 1
    frame = dict(ts=svc.time[0], frame_id=str(uuid4()), person=True, posture='lying', location='floor',
                 confidence=.95, caption='Resident beside blue mug', pose={'x': 1, 'y': 2})
    frame.update(changes)
    return svc.ingest('brain.perception', frame)


def suspect(svc):
    observe(svc)
    observe(svc)
    assert svc.pending and svc.pending['phase'] == 'awaiting_playback'
    return svc.pending['event_id']


def say_command(svc):
    return next(item for item in svc.commands if item['cmd'] == 'say')


def complete_say(svc):
    command = say_command(svc)
    svc.command_receipt(command['command_id'], 'accepted', 'simulation')
    svc.command_receipt(command['command_id'], 'executing', 'simulation')
    svc.command_receipt(command['command_id'], 'completed', 'simulation')
    return command


def kinds(svc):
    return [event['kind'] for event in svc.events()]


def test_queueing_is_not_delivery_and_waits_for_playback(service):
    suspect(service)
    pending = service.pending
    command = say_command(service)
    assert command['status'] == 'queued'
    assert pending['say_command_id'] == command['command_id']
    assert pending['deadline_at'] is None
    assert pending['audio_deadline_at'] == service.clock() + 8000
    assert pending['audio_started_at'] is None
    assert kinds(service) == ['fall_suspected']


def test_completed_receipt_starts_eight_second_window(service):
    suspect(service)
    started = service.clock()
    command = complete_say(service)
    assert command['updated_at'] == started
    pending = service.pending
    assert pending['phase'] == 'awaiting_reply'
    assert pending['audio_started_at'] == started
    assert pending['deadline_at'] == started + 8000
    assert pending['audio_deadline_at'] is None
    service.tick(started + 7999)
    assert kinds(service) == ['fall_suspected']
    service.tick(started + 8000)
    # Same-timestamp events have no defined relative order.
    assert Counter(kinds(service)) == Counter({'fall_suspected': 1, 'checkin_no_reply': 1, 'fall_confirmed': 1})


def test_accepted_and_executing_never_count_as_delivery(service):
    suspect(service)
    command = say_command(service)
    service.command_receipt(command['command_id'], 'accepted', 'simulation')
    service.command_receipt(command['command_id'], 'executing', 'simulation')
    assert service.pending['phase'] == 'awaiting_playback'
    assert service.pending['deadline_at'] is None


def test_duplicate_completed_receipt_cannot_restart_timer(service):
    suspect(service)
    complete_say(service)
    deadline = service.pending['deadline_at']
    started = service.pending['audio_started_at']
    service.time[0] += 4000
    repeat = service.command_receipt(say_command(service)['command_id'], 'completed', 'simulation')
    assert repeat['updated_at'] == started
    assert service.pending['deadline_at'] == deadline
    assert service.pending['audio_started_at'] == started


def test_wrong_command_cannot_start_window(service):
    suspect(service)
    other = service.queue_command({'cmd': 'look'})
    service.command_receipt(other['command_id'], 'accepted', 'simulation')
    service.command_receipt(other['command_id'], 'executing', 'simulation')
    service.command_receipt(other['command_id'], 'completed', 'simulation')
    assert service.pending['phase'] == 'awaiting_playback'
    assert service.pending['deadline_at'] is None


def test_playback_timeout_escalates_once_as_delivery_failure(service):
    suspect(service)
    deadline = service.pending['audio_deadline_at']
    service.tick(deadline - 1)
    assert kinds(service) == ['fall_suspected']
    service.tick(deadline)
    service.tick(deadline + 1)
    service.tick(deadline + 1000)
    assert Counter(kinds(service)) == Counter({'fall_suspected': 1, 'checkin_audio_failed': 1, 'fall_confirmed': 1})
    assert 'checkin_no_reply' not in kinds(service)
    assert service.pending is None


def test_failed_receipt_is_delivery_failure_and_escalates_once(service):
    suspect(service)
    command = say_command(service)
    service.command_receipt(command['command_id'], 'accepted', 'simulation')
    service.command_receipt(command['command_id'], 'failed', 'simulation', 'speaker unavailable')
    service.tick(service.clock() + 20000)
    assert Counter(kinds(service)) == Counter({'fall_suspected': 1, 'checkin_audio_failed': 1, 'fall_confirmed': 1})
    assert service.pending is None


def test_confirmed_audio_then_reply_resolves_checkin(service):
    event_id = suspect(service)
    complete_say(service)
    # Reassurance inside the verified window resolves the check-in.
    service.ingest('voice.heard', {'ts': service.clock(), 'text': 'okay', 'confidence': .99, 'event_id': event_id})
    assert Counter(kinds(service)) == Counter({'fall_suspected': 1, 'checkin_ok': 1})
    assert service.pending is None


def test_reassurance_before_playback_is_ignored_but_help_escalates(service):
    event_id = suspect(service)
    service.ingest('voice.heard', {'ts': service.clock(), 'text': 'okay', 'confidence': .99, 'event_id': event_id})
    assert kinds(service) == ['fall_suspected']
    assert service.pending['phase'] == 'awaiting_playback'
    service.ingest('voice.heard', {'ts': service.clock(), 'text': 'help', 'confidence': .99, 'event_id': event_id})
    assert Counter(kinds(service)) == Counter({'fall_suspected': 1, 'fall_confirmed': 1})
    assert service.pending is None


def test_default_mode_is_unchanged_queued_demo():
    now = [100000]
    svc = Service(':memory:', clock=lambda: now[0])
    try:
        for _ in range(2):
            now[0] += 1
            svc.ingest('brain.perception', {'ts': now[0], 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying',
                                            'location': 'floor', 'confidence': .95, 'caption': 'x', 'pose': {'x': 1, 'y': 2}})
        assert svc.pending['phase'] == 'queued_demo'
        assert svc.pending['delivery'] == 'unverified demo'
        assert svc.pending['deadline_at'] == svc.pending['started_at'] + 8000
        assert svc.status()['integrations']['voice'] == 'queued_only'
    finally:
        svc.close()


def test_create_app_env_and_status_reporting(monkeypatch):
    monkeypatch.setenv('ANNIE_REQUIRE_AUDIO_RECEIPT', 'true')
    with TestClient(create_app(':memory:', token='')) as client:
        assert client.get('/status').json()['integrations']['voice'] == 'receipt_required'
    monkeypatch.setenv('ANNIE_REQUIRE_AUDIO_RECEIPT', 'false')
    with TestClient(create_app(':memory:', token='')) as client:
        assert client.get('/status').json()['integrations']['voice'] == 'queued_only'


def test_enabled_mode_event_shape_and_audio_timeout_via_api():
    now = [100000]
    with TestClient(create_app(':memory:', clock=lambda: now[0], require_audio_receipt=True, token='')) as client:
        client.post('/demo/seed')
        client.post('/demo/scenario', json={'scenario': 'fall'})
        now[0] += 250
        client.post('/demo/scenario', json={'scenario': 'fall'})
        pending = client.get('/status').json()['pending_checkin']
        assert pending['phase'] == 'awaiting_playback'
        assert pending['audio_deadline_at'] - now[0] == 8000
        now[0] += 8000
        client.post('/demo/scenario', json={'scenario': 'timeout'})
        assert Counter(event['kind'] for event in client.get('/events').json()) == Counter(
            {'fall_suspected': 1, 'checkin_audio_failed': 1, 'fall_confirmed': 1})
