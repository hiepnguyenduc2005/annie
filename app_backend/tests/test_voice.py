import httpx
import pytest
from app.main import VoiceSettingsIn
from pydantic import ValidationError
from test_family_reliability import make_client, FamilyService


@pytest.mark.parametrize('value', ['true', 1])
def test_mute_requires_boolean(value):
    with pytest.raises(ValidationError):
        VoiceSettingsIn(muted=value)


def test_mute_is_forwarded_and_confirmed(monkeypatch):
    class Client:
        def __init__(self, **options):
            assert options['trust_env'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, json, headers):
            assert url.endswith('/voice')
            assert json == {'muted': True}
            assert headers == {'X-Body-Token': 'test-only'}
            return httpx.Response(200, json={'muted': True})
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    monkeypatch.setenv('ANNIE_BODY_TOKEN', 'test-only')
    with make_client(FamilyService(mock=True)) as client:
        response = client.post('/api/settings/voice', json={'muted': True})
        assert response.status_code == 200
        assert response.json()['muted'] is True
