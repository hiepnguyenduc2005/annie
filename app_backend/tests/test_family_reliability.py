"""Reliability of the family delivery path: honest queued state, real callbacks, dedup of
repeated sends, the inactivity deadline, and the family Pause. HTTP-level, in process."""
import time
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.family import FamilyService
from app.main import create_app


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError('no json body')
        return self._body


async def ok_dispatch(url, payload, timeout):
    return FakeResponse(200)  # legacy stand-in: 2xx without a body


async def queued_dispatch(url, payload, timeout):
    if url.endswith('/pause'):
        return FakeResponse(200, {'stop_confirmed': True})
    return FakeResponse(202, {'state': 'queued'})


def test_new_message_cannot_race_an_inflight_pause():
    entered, release = threading.Event(), threading.Event()

    async def dispatch(url, payload, timeout):
        if url.endswith('/pause'):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return FakeResponse(200, {'stop_confirmed': True})
        return FakeResponse(202, {'state': 'queued'})

    family = FamilyService(robot_backend_url='http://robot.example', dispatch_fn=dispatch)
    with make_client(family) as client, ThreadPoolExecutor() as executor:
        future = executor.submit(client.post, '/api/family/pause', json={})
        try:
            assert entered.wait(1)
            assert post(client, 'new instruction').status_code == 409
        finally:
            release.set()
        assert future.result(2).json()['stop_confirmed'] is True
        assert post(client, 'new instruction').status_code == 202


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


def post(client, text, **extra):
    return client.post('/api/messages', json={'author_id': 'zach', 'text': text, **extra})


def test_queued_dispatch_state_is_not_reported_as_running():
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=queued_dispatch)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = post(client, 'hi mom').json()['run_id']
        wait_for(lambda: client.get(f'/api/runs/{run_id}').json()['status'] == 'queued')
        run = client.get(f'/api/runs/{run_id}').json()
        assert run['status'] == 'queued' and run['events'] == []
        # The first real callback is what moves the run to running.
        client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                    json={'run_id': run_id, 'kind': 'navigating', 'payload': {}, 'at': 1})
        assert client.get(f'/api/runs/{run_id}').json()['status'] == 'running'


def test_callbacks_update_real_state_through_completion():
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=ok_dispatch)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = post(client, 'how are you').json()['run_id']
        headers = {'X-Internal-Secret': 's3cret'}
        for kind, payload, at in [('navigating', {}, 1), ('speaking', {'text': 'Zach asks how you are'}, 2)]:
            response = client.post('/internal/events', headers=headers,
                                   json={'run_id': run_id, 'kind': kind, 'payload': payload, 'at': at})
            assert response.status_code == 202
        done = client.post('/internal/events', headers=headers,
                           json={'run_id': run_id, 'kind': 'completed',
                                 'payload': {'detail': 'Delivered.', 'reply': 'okay'}, 'at': 3})
        assert done.status_code == 202
        run = client.get(f'/api/runs/{run_id}').json()
        assert run['status'] == 'completed'
        assert [event['kind'] for event in run['events']] == ['navigating', 'speaking', 'completed']
        assert run['outcome']['delivered'] is True and run['outcome']['reply'] == 'okay'


def test_duplicate_active_message_returns_the_same_run():
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=ok_dispatch)
    with make_client(family) as client:
        first = post(client, 'take your pills', reminder_id=3)
        second = post(client, 'take your pills', reminder_id=3)
        assert second.json()['run_id'] == first.json()['run_id']
        assert second.json()['deduplicated'] is True
        assert len(client.get('/api/thread').json()) == 1
        # A different text or a different reminder is a different errand.
        assert post(client, 'take your pills', reminder_id=4).json()['run_id'] != first.json()['run_id']
        assert post(client, 'drink some water', reminder_id=3).json()['run_id'] != first.json()['run_id']


def test_snapshot_includes_other_family_members_and_their_correlated_reminder():
    family = FamilyService(robot_backend_url='http://robot.example', dispatch_fn=ok_dispatch)
    with make_client(family) as client:
        response = client.post('/api/messages', json={'author_id': 'ellis', 'text': 'Charge your phone', 'reminder_id': 4})
        run_id = response.json()['run_id']
        snapshot = client.get('/api/family/snapshot').json()
        assert snapshot['thread'][0]['run_id'] == snapshot['runs'][0]['run_id'] == run_id
        assert snapshot['runs'][0]['reminder_id'] == 4
        assert snapshot['thread'][0]['author_id'] == 'ellis'


def test_identical_resend_after_terminal_outcome_is_a_new_run():
    async def refused(url, payload, timeout):
        raise httpx.ConnectError('connection refused')
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=refused)
    with make_client(family) as client:
        first = post(client, 'hello').json()['run_id']
        wait_for(lambda: client.get(f'/api/runs/{first}').json()['status'] == 'unreachable')
        second = post(client, 'hello').json()
        assert second['run_id'] != first
        assert 'deduplicated' not in second


def test_deadline_fails_a_run_that_goes_silent():
    clock = [1_700_000_000_000]
    family = FamilyService(clock=lambda: clock[0], robot_backend_url='http://robot.example:8001',
                           dispatch_fn=ok_dispatch, event_deadline_s=90)
    with make_client(family, clock=lambda: clock[0], internal_secret='s3cret') as client:
        run_id = post(client, 'are you okay').json()['run_id']
        wait_for(lambda: client.get(f'/api/runs/{run_id}').json()['status'] == 'dispatched')
        clock[0] += 91_000  # no callback ever arrived
        run = wait_for(lambda: (r := client.get(f'/api/runs/{run_id}').json())['status'] == 'failed' and r)
        assert run['events'][-1]['kind'] == 'failed'
        assert 'No word from Annie' in run['events'][-1]['payload']['error']
        assert run['outcome']['delivered'] is False
        # Terminal now: a late callback is rejected, not resurrected.
        late = client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                           json={'run_id': run_id, 'kind': 'completed', 'payload': {}, 'at': clock[0]})
        assert late.status_code == 409


def test_pause_cancels_live_runs_and_confirms_the_robot_stop():
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=queued_dispatch)
    with make_client(family, internal_secret='s3cret') as client:
        first = post(client, 'first').json()['run_id']
        second = post(client, 'second').json()['run_id']
        for run_id in (first, second):
            wait_for(lambda rid=run_id: client.get(f'/api/runs/{rid}').json()['status'] == 'queued')
        receipt = client.post('/api/family/pause', json={}).json()
        assert receipt['paused'] is True and receipt['stop_confirmed'] is True
        assert set(receipt['cancelled_runs']) == {first, second}
        for run_id in (first, second):
            run = client.get(f'/api/runs/{run_id}').json()
            assert run['status'] == 'cancelled'
            assert run['outcome']['type'] == 'cancelled' and run['outcome']['delivered'] is False
        # A late callback for a cancelled run is rejected.
        late = client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                           json={'run_id': first, 'kind': 'navigating', 'payload': {}, 'at': 1})
        assert late.status_code == 409
        # Resume is a new explicit message; nothing replays on its own.
        resumed = post(client, 'third').json()
        assert resumed['run_id'] not in (first, second)
        assert family.paused is False


def test_pause_reports_stop_unconfirmed_when_the_errand_refuses():
    async def pause_refused(url, payload, timeout):
        return FakeResponse(404) if url.endswith('/pause') else FakeResponse(202, {'state': 'queued'})
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=pause_refused)
    with make_client(family) as client:
        run_id = post(client, 'hi').json()['run_id']
        wait_for(lambda: client.get(f'/api/runs/{run_id}').json()['status'] == 'queued')
        receipt = client.post('/api/family/pause', json={}).json()
        assert receipt['paused'] is True and receipt['stop_confirmed'] is False
        assert receipt['cancelled_runs'] == [run_id]


@pytest.mark.parametrize('body', [None, {}, {'stopped': True}, {'stop_confirmed': 'false'}, ['stop_confirmed']])
def test_pause_stop_confirmed_only_on_explicit_json_true(body):
    async def vague_pause(url, payload, timeout):
        return FakeResponse(200, body) if url.endswith('/pause') else FakeResponse(202, {'state': 'queued'})
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=vague_pause)
    with make_client(family) as client:
        run_id = post(client, 'hi').json()['run_id']
        wait_for(lambda: client.get(f'/api/runs/{run_id}').json()['status'] == 'queued')
        receipt = client.post('/api/family/pause', json={}).json()
        assert receipt['paused'] is True and receipt['stop_confirmed'] is False


def test_pause_without_errand_url_cancels_but_cannot_confirm_a_stop():
    family = FamilyService(robot_backend_url='', dispatch_fn=ok_dispatch)
    with make_client(family) as client:
        receipt = client.post('/api/family/pause', json={}).json()
        assert receipt == {'paused': True, 'cancelled_runs': [], 'stop_confirmed': False}


def test_queued_answer_never_downgrades_a_run_that_already_started():
    import asyncio

    async def slow_queued(url, payload, timeout):
        await asyncio.sleep(0.3)
        return FakeResponse(202, {'state': 'queued'})
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=slow_queued)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = post(client, 'hi').json()['run_id']
        # The errand starts and reports before its own 202 reaches app_backend.
        client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                    json={'run_id': run_id, 'kind': 'navigating', 'payload': {}, 'at': 1})
        assert client.get(f'/api/runs/{run_id}').json()['status'] == 'running'
        time.sleep(0.6)  # the dispatch task has now applied its late queued answer
        assert client.get(f'/api/runs/{run_id}').json()['status'] == 'running'


def test_pause_in_mock_mode_stops_the_sequence_without_http():
    family = FamilyService(mock=True, mock_speed=0.5)
    with make_client(family) as client:
        run_id = post(client, 'slow mock').json()['run_id']
        receipt = client.post('/api/family/pause', json={}).json()
        assert receipt == {'paused': True, 'cancelled_runs': [run_id], 'stop_confirmed': True}
        run = client.get(f'/api/runs/{run_id}').json()
        assert run['status'] == 'cancelled' and run['events'] == []


def test_cancelled_event_kind_closes_the_run():
    # How an errand service shutdown or errand-side pause resolves a run instead of stranding it.
    family = FamilyService(robot_backend_url='http://robot.example:8001', dispatch_fn=ok_dispatch)
    with make_client(family, internal_secret='s3cret') as client:
        run_id = post(client, 'hi').json()['run_id']
        response = client.post('/internal/events', headers={'X-Internal-Secret': 's3cret'},
                               json={'run_id': run_id, 'kind': 'cancelled',
                                     'payload': {'error': 'Annie was paused.'}, 'at': 1})
        assert response.status_code == 202
        run = client.get(f'/api/runs/{run_id}').json()
        assert run['status'] == 'cancelled' and run['outcome']['type'] == 'cancelled'


def test_dog_status_passes_through_motion_enabled_and_paused(monkeypatch):
    telemetry = {'connected': True, 'source': 'hardware', 'state': {'mode': 'idle', 'tracks': []},
                 'motion_enabled': False, 'paused': True}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert url.endswith('/telemetry.json')
            return SimpleNamespace(status_code=200, json=lambda: telemetry)

    monkeypatch.setattr(httpx, 'AsyncClient', FakeAsyncClient)
    with make_client(FamilyService(mock=True)) as client:
        status = client.get('/api/dog/status').json()
        assert status['available'] is True
        assert status['motion_enabled'] is False and status['paused'] is True

    class DownAsyncClient(FakeAsyncClient):
        async def get(self, url):
            raise httpx.ConnectError('dog process down')

    monkeypatch.setattr(httpx, 'AsyncClient', DownAsyncClient)
    with make_client(FamilyService(mock=True)) as client:
        status = client.get('/api/dog/status').json()
        assert status['available'] is False
        assert status['motion_enabled'] is None and status['paused'] is None
