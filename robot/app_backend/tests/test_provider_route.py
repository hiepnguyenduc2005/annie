import importlib

from fastapi.testclient import TestClient

main = importlib.import_module('robot.app_backend.app.main')


def test_agents_require_explicit_egress_and_configuration(monkeypatch):
    monkeypatch.delenv('SUBCONSCIOUS_API_KEY', raising=False)
    monkeypatch.setenv('ANNIE_ENABLE_CLOUD_AGENTS', 'false')
    with TestClient(main.create_app(':memory:', token='')) as client:
        assert client.post('/agents/run', json={'task': 'Review this'}).status_code == 422
        assert client.post('/agents/run', json={'task': 'Review this', 'allow_cloud': True}).status_code == 503


def test_agents_route_auth_and_mocked_success(monkeypatch):
    calls = []
    async def mock_run(task, evidence, **kwargs):
        calls.append((task, evidence))
        return {'answer': 'Limited evidence.', 'used_frame_ids': [], 'agents': [], 'calls_used': 3}
    monkeypatch.setattr(main, 'run_team', mock_run)
    monkeypatch.setenv('SUBCONSCIOUS_API_KEY', 'synthetic-test-key')
    monkeypatch.setenv('ANNIE_ENABLE_CLOUD_AGENTS', 'true')
    with TestClient(main.create_app(':memory:', token='test-token')) as client:
        payload = {'task': 'Summarize evidence', 'evidence': [], 'allow_cloud': True}
        assert client.post('/agents/run', json=payload).status_code == 401
        response = client.post('/agents/run', json=payload, headers={'Authorization': 'Bearer test-token'})
        assert response.status_code == 200
        assert response.json()['calls_used'] == 3
        assert calls == [('Summarize evidence', [])]


def test_schema_is_authenticated_and_rebinding_host_rejected():
    with TestClient(main.create_app(':memory:', token='test-token')) as client:
        assert client.get('/openapi.json').status_code == 401
        response = client.get('/openapi.json', headers={'Authorization': 'Bearer test-token'})
        assert '/agents/run' in response.json()['paths']
    with TestClient(main.create_app(':memory:', token='')) as client:
        assert client.get('/status', headers={'Host': 'attacker.example'}).status_code == 400
