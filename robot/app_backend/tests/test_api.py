from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from robot.app_backend.app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(':memory:', token='')) as client:
        yield client


def test_empty_start_seed_commands_and_query(client):
    assert client.get('/').json() == {'message': 'Welcome to the Annie API'}
    assert client.get('/health').json() == {'status': 'ok'}
    assert client.get('/status').json()['dog'] is None
    assert client.get('/map').status_code == 404
    assert client.post('/commands', json={'cmd': 'goto', 'waypoint': 'x'}).status_code == 422
    assert client.post('/demo/seed').status_code == 200
    assert client.get('/map').json()['map_id'] == 'demo-home'
    response = client.post('/commands', json={'cmd': 'goto', 'waypoint': 'bedroom'})
    assert response.json()['status'] == 'queued'
    assert client.post('/say', json={'text': 'Hello'}).json()['status'] == 'queued'
    assert len(client.get('/commands').json()) == 2
    assert client.post('/query', json={'text': 'glasses'}).json()['answerable']
    assert client.post('/query', json={'text': 'elephant'}).json()['answer'] == 'I wasn’t there for that'
    assert client.get(f'/frames/{uuid4()}').status_code == 404


def test_ingest_validation_and_raw_frame_rejection(client):
    assert client.post('/ingest', json={'channel': 'dog.frame', 'data': {}}).status_code == 422
    from robot.app_backend.app.service import now_ms
    frame = {'ts': now_ms(), 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying', 'location': 'bed', 'confidence': .9, 'caption': 'resting', 'pose': {'x': 1, 'y': 2}}
    for extra in ({'jpeg_b64': 'raw'}, {'confidence': 1.2}, {'ts': '123'}):
        assert client.post('/ingest', json={'channel': 'brain.perception', 'data': {**frame, **extra}}).status_code == 422
    assert client.post('/ingest', json={'channel': 'brain.perception', 'data': frame}).json()['accepted']
    assert not client.post('/ingest', json={'channel': 'brain.perception', 'data': frame}).json()['accepted']
    assert 'jpeg_b64' not in str(client.get('/status').json())
    assert client.post('/say', json={'text': 'x' * 2001}).status_code == 422
    assert client.post('/say', content='x' * 600001).status_code == 413


def test_token_and_origin():
    with TestClient(create_app(':memory:', token='secret')) as client:
        assert client.get('/status').status_code == 401
        assert client.get('/status', headers={'Authorization': 'Bearer secret'}).status_code == 200
        assert client.get('/status', headers={'Authorization': 'Bearer secret', 'Origin': 'https://evil.example'}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/live') as ws:
                ws.send_json({'token': 'wrong'})
                ws.receive_json()
        with client.websocket_connect('/live') as ws:
            ws.send_json({'token': 'secret'})
            assert ws.receive_json()['type'] == 'snapshot'


def test_remote_rejected_even_with_forwarded_header():
    with TestClient(create_app(':memory:', token=''), client=('198.51.100.1', 42)) as client:
        assert client.get('/status', headers={'X-Forwarded-For': '127.0.0.1'}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/live'):
                pass


def test_websocket_updates(client):
    with client.websocket_connect('/live', headers={'Origin': 'http://testserver'}) as ws:
        assert ws.receive_json()['type'] == 'snapshot'
        client.post('/say', json={'text': 'Hello'})
        update = ws.receive_json()
        assert update['type'] == 'command'
        assert update['data']['status'] == 'queued'


def test_live_mode_has_no_demo_controls():
    with TestClient(create_app(':memory:', mode='live', token='')) as client:
        assert client.post('/demo/seed').status_code == 403
        assert client.post('/demo/scenario', json={'scenario': 'fall'}).status_code == 403


def test_scenario_event_ack_and_since():
    with TestClient(create_app(':memory:', clock=lambda: 100000, token='')) as client:
        assert client.post('/demo/scenario', json={'scenario': 'fall'}).json()['pending_checkin']
        events = client.get('/events').json()
        assert len(events) == 1
        event_id = events[0]['event_id']
        ack = client.post(f'/events/{event_id}/ack', json={'by': 'family'}).json()
        assert ack['acknowledged']
        assert client.post(f'/events/{event_id}/ack', json={'by': 'family'}).json() == ack
        assert client.get('/events?since=100000').json() == []
        client.post('/demo/scenario', json={'scenario': 'timeout'})
        assert len(client.get('/events').json()) == 3


def test_background_ticker_runs_without_request():
    now = [100000]
    with TestClient(create_app(':memory:', clock=lambda: now[0], token='')) as client:
        client.post('/demo/scenario', json={'scenario': 'fall'})
        with client.websocket_connect('/live') as ws:
            assert ws.receive_json()['data']['pending_checkin']
            now[0] += 8001
            # Blocking receive proves ticker emits without another HTTP request.
            assert ws.receive_json()['data']['kind'] == 'checkin_no_reply'
            assert ws.receive_json()['data']['kind'] == 'fall_confirmed'


def test_websocket_rejects_cross_origin_and_query_token(client):
    for path, headers in [('/live', {'Origin': 'http://evil.example'}), ('/live?token=secret', {})]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers=headers):
                pass
