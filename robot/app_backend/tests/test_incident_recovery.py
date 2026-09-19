"""Incident durability: episode, check-in and command state survive restart
without re-asking the question or double-escalating, and a resolved episode
re-arms only on affirmative recovery evidence or an explicit operator reset."""
from uuid import uuid4

import pytest

from robot.app_backend.app.service import Service


@pytest.fixture
def now():
    return [200000]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / 'incident.sqlite3')


def make(db_path, now, **kwargs):
    svc = Service(db_path, clock=lambda: now[0], **kwargs)
    svc.time = now
    return svc


def restart(db_path, now, **kwargs):
    svc = Service(db_path, clock=lambda: now[0], **kwargs)
    svc.time = now
    return svc


def frame(svc, **changes):
    svc.time[0] += 250 if svc.require_audio_receipt else 1
    data = dict(ts=svc.time[0], frame_id=str(uuid4()), person=True, posture='lying', location='floor',
                confidence=.95, caption='Resident beside blue mug', pose={'x': 1, 'y': 2})
    data.update(changes)
    svc.ingest('brain.perception', data)


def suspect(svc):
    frame(svc)
    frame(svc)
    assert svc.pending
    return svc.pending['event_id']


def kinds(svc):
    return [event['kind'] for event in svc.events()]


def test_pending_playback_survives_restart_without_reasking(db_path, now):
    svc = make(db_path, now, require_audio_receipt=True)
    suspect(svc)
    event_id = svc.pending['event_id']
    command_id = svc.pending['say_command_id']
    svc.close()
    restored = restart(db_path, now, require_audio_receipt=True)
    try:
        # Same episode resumes; the question command is preserved, not re-queued.
        assert restored.pending['phase'] == 'awaiting_playback'
        assert restored.pending['event_id'] == event_id
        assert restored.commands and restored.commands[-1]['command_id'] == command_id
        assert len([item for item in restored.commands if item['cmd'] == 'say']) == 1
        # Playback completion after restart still opens the reply window.
        now[0] += 1000
        restored.command_receipt(command_id, 'accepted', 'simulation')
        restored.command_receipt(command_id, 'executing', 'simulation')
        restored.command_receipt(command_id, 'completed', 'simulation')
        assert restored.pending['phase'] == 'awaiting_reply'
        assert restored.pending['deadline_at'] == now[0] + 8000
    finally:
        restored.close()


def test_restart_duplicate_ticks_do_not_double_escalate(db_path, now):
    svc = make(db_path, now, require_audio_receipt=True)
    suspect(svc)
    deadline = svc.pending['audio_deadline_at']
    svc.tick(deadline)
    assert kinds(svc).count('fall_confirmed') == 1
    svc.close()
    restored = restart(db_path, now, require_audio_receipt=True)
    try:
        assert restored.pending is None  # Escalation already recorded.
        restored.tick(deadline + 1)
        restored.tick(deadline + 1000)
        restored.tick(deadline + 100000)
        assert kinds(restored).count('fall_confirmed') == 1
        assert kinds(restored).count('checkin_audio_failed') == 1
    finally:
        restored.close()


def test_awaiting_reply_survives_restart_and_resolves_once(db_path, now):
    svc = make(db_path, now, require_audio_receipt=True)
    suspect(svc)
    command_id = svc.pending['say_command_id']
    now[0] += 500
    for status in ('accepted', 'executing', 'completed'):
        svc.command_receipt(command_id, status, 'simulation')
    deadline = svc.pending['deadline_at']
    svc.close()
    restored = restart(db_path, now, require_audio_receipt=True)
    try:
        assert restored.pending['phase'] == 'awaiting_reply'
        assert restored.pending['deadline_at'] == deadline
        now[0] = deadline + 1
        restored.tick()
        assert kinds(restored).count('checkin_no_reply') == 1
        assert kinds(restored).count('fall_confirmed') == 1
        restored.tick(now[0] + 5000)
        assert kinds(restored).count('fall_confirmed') == 1
        assert len([item for item in restored.commands if item['cmd'] == 'say']) == 1
    finally:
        restored.close()


def test_reassurance_survives_restart_within_restored_window(db_path, now):
    svc = make(db_path, now, require_audio_receipt=True)
    event_id = suspect(svc)
    command_id = svc.pending['say_command_id']
    now[0] += 500
    for status in ('accepted', 'executing', 'completed'):
        svc.command_receipt(command_id, status, 'simulation')
    svc.close()
    restored = restart(db_path, now, require_audio_receipt=True)
    try:
        now[0] += 1000
        restored.ingest('voice.heard', {'ts': now[0], 'text': 'okay', 'confidence': .99, 'event_id': event_id})
        assert restored.pending is None
        assert kinds(restored).count('checkin_ok') == 1
    finally:
        restored.close()


def test_empty_frame_does_not_reset_episode_but_two_present_safe_captures_do(db_path, now):
    svc = make(db_path, now)
    suspect(svc)
    svc.tick(svc.pending['deadline_at'])
    assert svc.episode  # Unresolved concern survives; empty frames prove nothing.
    svc.ingest('brain.perception', {'ts': now[0] + 1, 'frame_id': str(uuid4()), 'person': False,
                                    'posture': 'standing', 'location': 'unknown', 'confidence': .95,
                                    'caption': 'empty room', 'pose': {'x': 1, 'y': 2}})
    assert svc.episode  # Empty frame is not recovery evidence.
    frame(svc, posture='standing', location='bed')
    now[0] += 600
    frame(svc, posture='standing', location='bed')
    assert not svc.episode  # Two present, affirmatively safe captures re-arm.
    suspect(svc)  # After re-arm a new suspicion produces a fresh check-in.
    assert svc.pending
    svc.close()


def test_safe_captures_must_be_close_and_spaced(db_path, now):
    svc = make(db_path, now)
    suspect(svc)
    svc.tick(svc.pending['deadline_at'])
    frame(svc, posture='standing', location='bed')
    now[0] += 6000  # Outside the 5 s pairing window.
    frame(svc, posture='standing', location='bed')
    assert svc.episode
    now[0] += 100  # Inside window but under the 500 ms spacing.
    frame(svc, posture='standing', location='bed')
    assert svc.episode
    now[0] += 600  # Proper spacing from the previous capture.
    frame(svc, posture='standing', location='bed')
    assert not svc.episode
    svc.close()


def test_low_confidence_capture_does_not_rearm(db_path, now):
    svc = make(db_path, now)
    suspect(svc)
    svc.tick(svc.pending['deadline_at'])
    frame(svc, confidence=.5, posture='standing', location='bed')
    now[0] += 600
    frame(svc, confidence=.5, posture='standing', location='bed')
    assert svc.episode
    svc.close()


def test_demo_safe_bed_reset_rearms_without_waiting(db_path, now):
    svc = make(db_path, now)
    suspect(svc)
    svc.tick(svc.pending['deadline_at'])
    assert svc.episode
    svc.scenario('safe_bed')  # Explicit operator reset for the demo.
    assert not svc.episode
    suspect(svc)
    assert svc.pending
    svc.close()


def test_episode_flag_survives_restart(db_path, now):
    svc = make(db_path, now)
    suspect(svc)
    svc.tick(svc.pending['deadline_at'])
    assert svc.episode
    svc.close()
    restored = restart(db_path, now)
    try:
        assert restored.episode  # Blocked re-arm state is durable.
        restored.ingest('brain.perception', {'ts': now[0] + 1, 'frame_id': str(uuid4()), 'person': False,
                                            'posture': 'standing', 'location': 'unknown', 'confidence': .95,
                                            'caption': 'empty room', 'pose': {'x': 1, 'y': 2}})
        assert restored.episode
    finally:
        restored.close()


class CrashAfterEventInsert(Exception):
    pass


def test_crash_between_escalation_event_and_pending_clear_is_atomic(db_path, now):
    """Injected crash between the event INSERT and pending clear persists
    nothing: reopening shows no fall_confirmed and the check-in still pending,
    then the retry escalates exactly once."""
    svc = make(db_path, now, require_audio_receipt=True)
    suspect(svc)
    deadline = svc.pending['audio_deadline_at']
    original = svc.event
    def crashing_event(kind, severity, evidence, ts=None):
        event = original(kind, severity, evidence, ts=ts)
        if kind == 'checkin_audio_failed':
            raise CrashAfterEventInsert
        return event
    svc.event = crashing_event
    with pytest.raises(CrashAfterEventInsert):
        svc.tick(deadline)
    svc.event = original
    svc.close()
    reopened = restart(db_path, now, require_audio_receipt=True)
    try:
        # 0 or 1 (atomic): the crash left ZERO escalation evidence behind and
        # the pending check-in intact for retry.
        assert kinds(reopened).count('checkin_audio_failed') == 0
        assert kinds(reopened).count('fall_confirmed') == 0
        assert reopened.pending is not None
        assert reopened.pending['phase'] == 'awaiting_playback'
        assert reopened.pending['audio_deadline_at'] == deadline
        # Retry after crash escalates exactly once.
        reopened.tick(deadline)
        assert kinds(reopened).count('checkin_audio_failed') == 1
        assert kinds(reopened).count('fall_confirmed') == 1
        assert reopened.pending is None
    finally:
        reopened.close()


def test_crash_during_creation_leaves_no_orphan(db_path, now):
    """Crash after the fall_suspected event but before pending/episode save
    persists no half-created episode: no event, no pending, no episode."""
    svc = make(db_path, now, require_audio_receipt=True)
    frame(svc)
    original = svc.event
    def crashing_event(kind, severity, evidence, ts=None):
        event = original(kind, severity, evidence, ts=ts)
        if kind == 'fall_suspected':
            raise CrashAfterEventInsert
        return event
    svc.event = crashing_event
    with pytest.raises(CrashAfterEventInsert):
        frame(svc)
    svc.close()
    reopened = restart(db_path, now)
    try:
        assert reopened.events() == []
        assert reopened.pending is None
        assert not reopened.episode
        # Candidate continuity is in-memory, so a clean pair after restart
        # creates the check-in without resurrecting the crashed attempt.
        frame(reopened)
        frame(reopened)
        assert reopened.pending is not None
        assert kinds(reopened).count('fall_suspected') == 1
    finally:
        reopened.close()


def test_command_store_prunes_but_protects_pending_question(db_path, now):
    svc = make(db_path, now, require_audio_receipt=True)
    suspect(svc)
    say_id = svc.pending['say_command_id']
    for _ in range(150):
        svc.queue_command({'cmd': 'look'})
    assert len(svc.commands) == 100
    # The pending check-in question survives pruning.
    assert any(item['command_id'] == say_id for item in svc.commands)
    # Reload proves the durable store holds the same bounded set.
    svc.close()
    reopened = restart(db_path, now, require_audio_receipt=True)
    try:
        assert len(reopened.commands) == 100
        assert any(item['command_id'] == say_id for item in reopened.commands)
        assert reopened.pending['say_command_id'] == say_id
    finally:
        reopened.close()


def test_map_change_clears_live_perception_but_keeps_history(db_path, now):
    svc = make(db_path, now)
    frame(svc)
    assert svc.perception is not None
    updates = []
    import asyncio
    queue = asyncio.Queue()
    svc.subscribers.add(queue)
    svc.ingest('dog.map', {'ts': now[0], 'map_id': 'map-B', 'origin': {'x': 0, 'y': 0},
                           'resolution_m': .05, 'waypoints': []})
    assert svc.perception is None  # No stale capture shown under the new map.
    assert svc.map['map_id'] == 'map-B'
    # History rows are untouched: query still cites the old map capture.
    assert svc.query('mug')['answerable']
    while not queue.empty():
        updates.append(queue.get_nowait())
    assert updates[0] == {'type': 'brain.perception', 'data': None}
    svc.close()


def test_receipt_mode_requires_250ms_capture_separation(db_path, now):
    svc = make(db_path, now, require_audio_receipt=True)
    frame(svc)
    svc.ingest('brain.perception', {'ts': now[0], 'frame_id': str(uuid4()), 'person': True,
                                    'posture': 'lying', 'location': 'floor', 'confidence': .95,
                                    'caption': 'x', 'pose': {'x': 1, 'y': 2}})
    assert svc.pending is None  # Same-millisecond pair rejected in receipt mode.
    now[0] += 250
    svc.ingest('brain.perception', {'ts': now[0], 'frame_id': str(uuid4()), 'person': True,
                                    'posture': 'lying', 'location': 'floor', 'confidence': .95,
                                    'caption': 'x', 'pose': {'x': 1, 'y': 2}})
    assert svc.pending is not None  # Properly separated pair qualifies.
    svc.close()


def test_default_demo_still_accepts_same_timestamp_pair(db_path, now):
    svc = make(db_path, now)
    frame(svc)
    same_ts = dict(ts=now[0], frame_id=str(uuid4()), person=True, posture='lying',
                   location='floor', confidence=.95, caption='x', pose={'x': 1, 'y': 2})
    svc.ingest('brain.perception', same_ts)  # Legacy demo: tie accepted.
    assert svc.pending is not None
    svc.close()
