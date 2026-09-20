import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.family import FamilyService
from app.main import create_app


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


async def ok_dispatch(url, payload, timeout):
    return FakeResponse(200)


async def refused_dispatch(url, payload, timeout):
    raise httpx.ConnectError('connection refused')


async def bad_status_dispatch(url, payload, timeout):
    return FakeResponse(500)


def make_client(family_service=None, **kwargs):
    return TestClient(create_app(':memory:', token='', family_service=family_service, **kwargs))


def wait_for(predicate, timeout_s=5.0, interval_s=0.02):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError('condition never became true')


def test_post_message_returns_immediately_without_waiting_on_robot():
    # dispatch_fn would hang without a timeout; the request must still return fast.
    async def slow_dispatch(url, payload, timeout):
        raise httpx.ConnectError('unreachable host')
    family = FamilyService(robot_backend_url='http://10.0.0.99:8001', dispatch_fn=slow_dispatch)
    with make_client(family) as client:
        started = time.monotonic()
        response = client.post('/api/messages', json={'author_id': 'zach', 'text': 'Hi Mom'})
        assert time.monotonic() - started < 1.0
        assert response.status_code == 202
        body = response.json()
        assert body['status'] == 'dispatched'
        assert body['run_id']


def test_unknown_author_rejected():
    with make_client(FamilyService(mock=True, mock_speed=0)) as client:
        assert client.post('/api/messages', json={'author_id': 'nobody', 'text': 'hi'}).status_code == 422


def test_dispatch_202_stays_dispatched_until_first_real_callback():
    # An HTTP 202 only means the errand service accepted the run; 'running' is reserved
    # for actual progress reported through /internal/events.
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=ok_dispatch)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = client.post('/api/messages', json={'author_id': 'zach', 'text': 'hi'}).json()['run_id']
        time.sleep(0.1)  # let the dispatch background task finish
        assert client.get(f'/api/runs/{run_id}').json()['status'] == 'dispatched'
        client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                    json={'run_id': run_id, 'kind': 'navigating', 'payload': {}, 'at': 1})
        run = wait_for(lambda: (r := client.get(f'/api/runs/{run_id}').json())['status'] == 'running' and r)
        assert run['status'] == 'running'


@pytest.mark.parametrize('dispatch_fn', [refused_dispatch, bad_status_dispatch])
def test_unreachable_robot_never_produces_a_500_and_marks_run(dispatch_fn):
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=dispatch_fn)
    with make_client(family) as client:
        response = client.post('/api/messages', json={'author_id': 'zach', 'text': 'hi'})
        assert response.status_code == 202
        run_id = response.json()['run_id']
        run = wait_for(lambda: (r := client.get(f'/api/runs/{run_id}').json())['status'] == 'unreachable' and r)
        assert run['events'][-1]['kind'] == 'unreachable'


def test_missing_robot_backend_url_marks_unreachable_without_calling_dispatch():
    calls = []
    async def spy(url, payload, timeout):
        calls.append(url)
        return FakeResponse(200)
    family = FamilyService(robot_backend_url='', dispatch_fn=spy)
    with make_client(family) as client:
        run_id = client.post('/api/messages', json={'author_id': 'ellis', 'text': 'hi'}).json()['run_id']
        wait_for(lambda: client.get(f'/api/runs/{run_id}').json()['status'] == 'unreachable')
    assert calls == []


def test_mock_mode_runs_full_sequence_to_completion():
    family = FamilyService(mock=True, mock_speed=0.01)
    with make_client(family) as client:
        run_id = client.post('/api/messages', json={'author_id': 'ellis', 'text': 'checking in'}).json()['run_id']
        run = wait_for(lambda: (r := client.get(f'/api/runs/{run_id}').json())['status'] == 'completed' and r, timeout_s=5)
        kinds = [event['kind'] for event in run['events']]
        assert kinds[0] == 'navigating'
        assert kinds[-1] == 'completed'
        assert 'speaking' in kinds and 'listening' in kinds and 'recalled' in kinds


def test_thread_lists_messages_in_order():
    with make_client(FamilyService(mock=True, mock_speed=0)) as client:
        client.post('/api/messages', json={'author_id': 'zach', 'text': 'first'})
        client.post('/api/messages', json={'author_id': 'ellis', 'text': 'second'})
        thread = client.get('/api/thread').json()
        assert [(m['author_id'], m['text']) for m in thread] == [('zach', 'first'), ('ellis', 'second')]


def test_get_run_404_for_unknown_run():
    with make_client(FamilyService(mock=True)) as client:
        assert client.get(f'/api/runs/{uuid4()}').status_code == 404


def test_internal_events_require_shared_secret():
    family = FamilyService(mock=True, mock_speed=0)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = client.post('/api/messages', json={'author_id': 'zach', 'text': 'a'}).json()['run_id']
        no_header = client.post('/internal/events', json={'run_id': run_id, 'kind': 'speaking', 'payload': {}, 'at': 0})
        assert no_header.status_code == 401
        wrong = client.post('/internal/events', headers={'X-Internal-Secret': 'nope'},
                             json={'run_id': run_id, 'kind': 'speaking', 'payload': {}, 'at': 0})
        assert wrong.status_code == 401


def test_internal_events_rejected_when_secret_unconfigured():
    with make_client(FamilyService(mock=True), internal_secret='') as client:
        response = client.post('/internal/events', headers={'X-Internal-Secret': ''},
                                json={'run_id': str(uuid4()), 'kind': 'speaking', 'payload': {}, 'at': 0})
        assert response.status_code == 401


def test_internal_events_update_run_and_reject_after_terminal():
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=ok_dispatch)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = client.post('/api/messages', json={'author_id': 'zach', 'text': 'a'}).json()['run_id']
        headers = {'X-Internal-Secret': 's3cret'}
        speaking = client.post('/internal/events', headers=headers,
                                json={'run_id': run_id, 'kind': 'speaking', 'payload': {'text': 'hi'}, 'at': 100})
        assert speaking.status_code == 202
        assert client.get(f'/api/runs/{run_id}').json()['status'] == 'running'
        done = client.post('/internal/events', headers=headers,
                            json={'run_id': run_id, 'kind': 'completed', 'payload': {}, 'at': 200})
        assert done.status_code == 202
        assert client.get(f'/api/runs/{run_id}').json()['status'] == 'completed'
        late = client.post('/internal/events', headers=headers,
                            json={'run_id': run_id, 'kind': 'speaking', 'payload': {}, 'at': 300})
        assert late.status_code == 409


def test_internal_events_unknown_run_is_404():
    with make_client(FamilyService(mock=True), internal_secret='s3cret') as client:
        response = client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                                json={'run_id': str(uuid4()), 'kind': 'speaking', 'payload': {}, 'at': 0})
        assert response.status_code == 404


def test_ws_family_sends_snapshot_then_live_message_and_run_updates():
    family = FamilyService(mock=True, mock_speed=0)
    with make_client(family) as client:
        with client.websocket_connect('/ws/family', headers={'Origin': 'http://testserver'}) as ws:
            snapshot = ws.receive_json()
            assert snapshot['type'] == 'snapshot'
            assert snapshot['data'] == {'thread': [], 'runs': []}
            client.post('/api/messages', json={'author_id': 'zach', 'text': 'ping'})
            types = {ws.receive_json()['type'] for _ in range(2)}
            assert types == {'message', 'run_status'}
