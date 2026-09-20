import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import voice


@pytest.mark.parametrize('value', ['true', 1, None])
def test_mute_requires_boolean(value):
    app = FastAPI()
    app.include_router(voice.router)
    with TestClient(app) as client:
        assert client.post('/api/settings/voice', json={'muted': value}).status_code == 422


def test_remote_mute_confirmed_and_failures(monkeypatch):
    state = {'muted': False}
    calls = []
    class Client:
        def __init__(self, **options):
            assert options['trust_env'] is False
            assert options['timeout'] == 3.0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, headers):
            return httpx.Response(200, json=state, request=httpx.Request('GET', url))
        async def post(self, url, json, headers):
            calls.append((json, headers))
            state.update(json)
            return await self.get(url, headers)
    monkeypatch.setattr(voice.httpx, 'AsyncClient', Client)
    monkeypatch.setenv('ANNIE_BODY_TOKEN', 'test-only')
    app = FastAPI()
    app.include_router(voice.router)
    with TestClient(app) as client:
        assert client.get('/api/settings/voice').json()['muted'] is False
        assert client.post('/api/settings/voice', json={'muted': True}).json()['muted'] is True
        assert calls == [({'muted': True}, {'X-Body-Token': 'test-only'})]
        state.clear()
        assert client.get('/api/settings/voice').status_code == 503
