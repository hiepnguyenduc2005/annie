from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from robot.app_backend.app.main import create_app


def queue_goto(client):
    client.post('/demo/seed')
    return client.post('/commands', json={'cmd': 'goto', 'waypoint': 'bedroom'}).json()


@pytest.fixture
def client():
    with TestClient(create_app(':memory:', token='')) as client:
        yield client


def test_receipt_unknown_command_is_404(client):
    assert client.post(f'/commands/{uuid4()}/receipt', json={'status': 'accepted', 'source': 'simulation'}).status_code == 404


def test_receipt_validates_strict_body(client):
    command = queue_goto(client)
    url = f'/commands/{command["command_id"]}/receipt'
    for body in ({'status': 'accepted'}, {'status': 'accepted', 'source': 'simulation', 'detail': 'x' * 501},
                 {'status': 'running', 'source': 'simulation'}, {'status': 'accepted', 'source': 'hardware'},
                 {'status': 'accepted', 'source': 'simulation', 'extra': 1}, {'status': 1, 'source': 'simulation'}):
        assert client.post(url, json=body).status_code == 422
    assert client.post(url, json={'status': 'accepted', 'source': 'simulation', 'detail': ''}).status_code == 200


def test_receipt_rejects_invalid_and_terminal_rewind_transitions(client):
    command = queue_goto(client)
    url = f'/commands/{command["command_id"]}/receipt'
    assert client.post(url, json={'status': 'completed', 'source': 'simulation'}).status_code == 409
    assert client.post(url, json={'status': 'accepted', 'source': 'simulation'}).status_code == 200
    assert client.post(url, json={'status': 'accepted', 'source': 'simulation'}).status_code == 409
    assert client.post(url, json={'status': 'executing', 'source': 'simulation'}).status_code == 200
    assert client.post(url, json={'status': 'completed', 'source': 'simulation'}).status_code == 200
    for status in ('accepted', 'executing', 'failed'):
        assert client.post(url, json={'status': status, 'source': 'simulation'}).status_code == 409


def test_receipt_idempotent_terminal_state(client):
    command = queue_goto(client)
    url = f'/commands/{command["command_id"]}/receipt'
    assert client.post(url, json={'status': 'failed', 'source': 'simulation', 'detail': 'waypoint unreachable'}).status_code == 200
    repeat = client.post(url, json={'status': 'failed', 'source': 'simulation', 'detail': 'waypoint unreachable'})
    assert repeat.status_code == 200
    assert repeat.json() == client.get('/commands').json()[-1]


def test_receipt_status_stream_preserves_ts_and_emits_updates():
    now = [1000]
    with TestClient(create_app(':memory:', clock=lambda: now[0], token='')) as client:
        with client.websocket_connect('/live', headers={'Origin': 'http://testserver'}) as ws:
            assert ws.receive_json()['type'] == 'snapshot'
            command = queue_goto(client)
            while True:
                update = ws.receive_json()
                if update['type'] == 'command' and update['data']['command_id'] == command['command_id']:
                    break
            url = f'/commands/{command["command_id"]}/receipt'
            statuses = ['accepted', 'executing', 'completed']
            for status in statuses:
                now[0] += 100
                receipt = client.post(url, json={'status': status, 'source': 'simulation', 'detail': f'now {status}'}).json()
                update = ws.receive_json()
                assert update['type'] == 'command'
                assert (update['data']['status'], update['data']['updated_at']) == (status, now[0])
                assert receipt['ts'] == command['ts'] and receipt['source'] == 'simulation'
            assert client.get('/commands').json()[-1]['detail'] == 'now completed'


def test_service_receipt_rejects_unknown_and_preserves_history():
    now = [5000]
    from robot.app_backend.app.service import Service
    service = Service(':memory:', clock=lambda: now[0])
    command = service.queue_command({'cmd': 'stop'})
    try:
        service.command_receipt(str(uuid4()), 'accepted', 'simulation')
        assert False, 'unknown command must raise'
    except KeyError:
        pass
    assert service.command_receipt(command['command_id'], 'failed', 'simulation', 'no robot')['status'] == 'failed'
    before = now[0]
    now[0] += 100
    repeat = service.command_receipt(command['command_id'], 'failed', 'simulation', 'no robot')
    assert repeat['updated_at'] == before and repeat['detail'] == 'no robot'
