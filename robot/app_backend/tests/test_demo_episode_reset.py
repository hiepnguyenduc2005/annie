"""Explicit rehearsal reset preserves evidence and cannot erase an active check-in."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from robot.app_backend.app.main import create_app
from robot.app_backend.app.service import Service


def suspect(service, clock):
    for _ in range(2):
        clock[0] += 300
        assert service.ingest('brain.perception', {
            'ts': clock[0], 'frame_id': str(uuid4()), 'person': True,
            'posture': 'lying', 'location': 'floor', 'confidence': .95,
            'caption': 'Visible person lying on the floor', 'pose': {'x': 1, 'y': 2},
        })
    assert service.pending is not None


def resolved(service, clock):
    suspect(service, clock)
    command_id = service.pending['say_command_id']
    for state in ('accepted', 'executing', 'completed'):
        service.command_receipt(command_id, state, 'simulation')
    service.ingest('voice.heard', {
        'ts': clock[0], 'text': 'help', 'confidence': .99,
        'event_id': service.pending['event_id'],
    })
    assert service.pending is None and service.episode


def rows(service):
    return {table: service.db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
            for table in ('events', 'memory', 'commands')}


def test_reset_preserves_evidence_and_allows_second_incident_after_restart(tmp_path):
    path = str(tmp_path / 'episodes.sqlite3')
    clock = [100000]
    app = create_app(path, mode='demo', token='', clock=lambda: clock[0], require_audio_receipt=True)
    with TestClient(app) as client:
        service = app.state.service
        resolved(service, clock)
        before = rows(service)
        perception = service.perception.copy()
        service.candidate, service.candidate_map, service.rearm = clock[0], 'demo-home', [clock[0]]
        response = client.post('/demo/reset-episode')
        assert response.status_code == 200
        assert response.json() == {'reset': True, 'scope': 'resolved_demo_episode'}
        assert not service.episode and service.candidate is None and service.candidate_map is None
        assert service.rearm == []
        assert rows(service) == before
        assert service.perception == perception
    restored = Service(path, mode='demo', clock=lambda: clock[0], require_audio_receipt=True)
    try:
        assert not restored.episode
        assert rows(restored) == before
        suspect(restored, clock)
        assert len([e for e in restored.events() if e['kind'] == 'fall_suspected']) == 2
    finally:
        restored.close()


def test_pending_checkin_cannot_be_reset():
    clock = [100000]
    app = create_app(':memory:', mode='demo', token='', clock=lambda: clock[0], require_audio_receipt=True)
    with TestClient(app) as client:
        service = app.state.service
        suspect(service, clock)
        before, pending = rows(service), service.pending.copy()
        assert client.post('/demo/reset-episode').status_code == 409
        assert service.episode and service.pending == pending and rows(service) == before


def test_reset_requires_demo_mode_and_existing_auth_boundary():
    with TestClient(create_app(':memory:', mode='live', token='')) as client:
        assert client.post('/demo/reset-episode').status_code == 403
    with TestClient(create_app(':memory:', mode='demo', token='demo-secret')) as client:
        assert client.post('/demo/reset-episode').status_code == 401
        headers = {'Authorization': 'Bearer demo-secret'}
        assert client.post('/demo/reset-episode', headers={**headers, 'Origin': 'https://example.com'}).status_code == 403
        assert client.post('/demo/reset-episode', headers=headers).status_code == 200
    service = Service(':memory:', mode='live')
    try:
        with pytest.raises(PermissionError):
            service.reset_demo_episode()
    finally:
        service.close()


def test_reset_rolls_back_if_persistence_fails(monkeypatch):
    clock = [100000]
    service = Service(':memory:', mode='demo', clock=lambda: clock[0], require_audio_receipt=True)
    try:
        resolved(service, clock)
        service.candidate, service.candidate_map, service.rearm = clock[0], 'demo-home', [clock[0]]
        before = rows(service)
        save = service._save_episode
        def fail_after_write():
            save()
            raise RuntimeError('injected persistence failure')
        monkeypatch.setattr(service, '_save_episode', fail_after_write)
        with pytest.raises(RuntimeError, match='injected'):
            service.reset_demo_episode()
        assert service.episode and service.candidate == clock[0]
        assert service.candidate_map == 'demo-home' and service.rearm == [clock[0]]
        assert rows(service) == before
        assert service.db.execute("SELECT payload FROM state WHERE topic='episode'").fetchone()[0] == 'true'
    finally:
        service.close()
