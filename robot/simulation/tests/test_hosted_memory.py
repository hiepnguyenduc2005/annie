import asyncio
import json as jsonlib
import ssl

import httpx
import pytest
import certifi

from robot.app_backend.app.models import Perception, Pose
from robot.simulation.hosted_memory import (
    ElasticMemory,
    MemoryUnavailable,
    from_env,
)


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, response=None):
        self.response = response or {}
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(self.response.get('status', 200),
                              json=self.response.get('json', {}))


class FailingTransport(httpx.AsyncBaseTransport):
    def __init__(self, error):
        self.error = error
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        raise self.error


def perception(**overrides):
    fields = dict(
        source='simulation_vlm',
        ts=1700,
        frame_id=__import__('uuid').uuid4(),
        person=True,
        posture='sitting',
        location='chair',
        confidence=0.9,
        caption='resident sitting in chair',
        pose=Pose(x=1.0, y=2.0, yaw=0.5, map_id='sim-one'),
    )
    fields.update(overrides)
    return Perception(**fields)


def memory(transport, index='annie-sim-observations'):
    client = httpx.AsyncClient(transport=transport)
    return ElasticMemory('https://elastic.example:9243', 'test-key', index,
                         client=client)


def ca_env(monkeypatch, **values):
    for key in ('ANNIE_MEMORY_PROVIDER', 'ELASTIC_URL', 'ELASTIC_API_KEY',
                'ANNIE_MEMORY_INDEX', 'ELASTIC_CA_CERT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ANNIE_MEMORY_PROVIDER', 'elastic')
    monkeypatch.setenv('ELASTIC_URL', 'https://ok.example')
    monkeypatch.setenv('ELASTIC_API_KEY', 'secret')
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_from_env_propagates_ca_cert(monkeypatch):
    ca_env(monkeypatch, ELASTIC_CA_CERT=certifi.where())
    adapter = from_env()
    assert adapter is not None
    assert isinstance(adapter._verify, ssl.SSLContext)
    assert adapter._verify.verify_mode == ssl.CERT_REQUIRED


def test_from_env_without_ca_uses_default_verify(monkeypatch):
    ca_env(monkeypatch)
    adapter = from_env()
    assert adapter is not None
    assert adapter._verify is True


def test_blank_ca_env_treated_as_absent(monkeypatch):
    ca_env(monkeypatch, ELASTIC_CA_CERT='')
    adapter = from_env()
    assert adapter is not None
    assert adapter._verify is True


def test_missing_ca_file_fails_sanitized_before_http(monkeypatch):
    ca_env(monkeypatch, ELASTIC_CA_CERT='/nonexistent/path/ca.pem')
    with pytest.raises(MemoryUnavailable) as excinfo:
        from_env()
    message = str(excinfo.value)
    assert 'ca.pem' not in message
    assert '/nonexistent' not in message
    assert 'secret' not in message


def test_invalid_ca_file_fails_sanitized_before_http(monkeypatch, tmp_path):
    ca_file = tmp_path / 'bad.pem'
    ca_file.write_bytes(b'this is not a certificate')
    ca_env(monkeypatch, ELASTIC_CA_CERT=str(ca_file))
    with pytest.raises(MemoryUnavailable) as excinfo:
        from_env()
    message = str(excinfo.value)
    assert 'bad.pem' not in message
    assert 'PEM' not in message
    assert 'secret' not in message


def test_valid_ca_file_is_accepted():
    ca_file = certifi.where()
    adapter = ElasticMemory('https://elastic.example:9243', 'test-key',
                            'annie-sim-observations', ca_cert=str(ca_file))
    assert isinstance(adapter._verify, ssl.SSLContext)
    assert adapter._verify.verify_mode == ssl.CERT_REQUIRED


def test_https_still_required_with_ca():
    ca_file = certifi.where()
    with pytest.raises(MemoryUnavailable):
        ElasticMemory('http://elastic.example:9243', 'test-key',
                      'annie-sim-observations', ca_cert=str(ca_file))


def test_custom_verify_context_reaches_httpx_client(monkeypatch):
    ca_env(monkeypatch, ELASTIC_CA_CERT=certifi.where())
    adapter = from_env()
    assert adapter is not None
    original_client = httpx.AsyncClient
    seen = {}
    transport = RecordingTransport({'json': {'hits': {'hits': []}}})

    # One wrapper: capture kwargs and inject the recording transport so the
    # client never attempts a live request.
    def client_with_transport(*args, **kwargs):
        seen.update(kwargs)
        kwargs['transport'] = transport
        return original_client(*args, **kwargs)
    monkeypatch.setattr(
        'robot.simulation.hosted_memory.httpx.AsyncClient',
        client_with_transport)
    result = asyncio.run(adapter.search('sitting', 'sim-one'))
    assert result == []
    assert seen.get('verify') is not True
    assert isinstance(seen.get('verify'), ssl.SSLContext)
    assert seen.get('trust_env') is False
    assert seen.get('follow_redirects') is False


def test_from_env_disabled_without_provider(monkeypatch):
    for key in ('ANNIE_MEMORY_PROVIDER', 'ELASTIC_URL', 'ELASTIC_API_KEY',
                'ANNIE_MEMORY_INDEX', 'ELASTIC_CA_CERT'):
        monkeypatch.delenv(key, raising=False)
    assert from_env() is None


def test_from_env_rejects_partial_or_unsafe_config(monkeypatch):
    for key in ('ANNIE_MEMORY_PROVIDER', 'ELASTIC_URL', 'ELASTIC_API_KEY',
                'ANNIE_MEMORY_INDEX', 'ELASTIC_CA_CERT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ANNIE_MEMORY_PROVIDER', 'elastic')
    with pytest.raises(MemoryUnavailable):
        from_env()
    monkeypatch.setenv('ELASTIC_URL', 'http://insecure.example')
    with pytest.raises(MemoryUnavailable):
        from_env()
    monkeypatch.setenv('ELASTIC_URL', 'https://ok.example')
    monkeypatch.setenv('ANNIE_MEMORY_INDEX', 'Bad Index!')
    with pytest.raises(MemoryUnavailable):
        from_env()


def test_from_env_builds_adapter_with_default_index(monkeypatch):
    for key in ('ANNIE_MEMORY_PROVIDER', 'ELASTIC_URL', 'ELASTIC_API_KEY',
                'ANNIE_MEMORY_INDEX', 'ELASTIC_CA_CERT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ANNIE_MEMORY_PROVIDER', 'elastic')
    monkeypatch.setenv('ELASTIC_URL', 'https://ok.example')
    monkeypatch.setenv('ELASTIC_API_KEY', 'secret')
    adapter = from_env()
    assert adapter is not None
    assert adapter._index == 'annie-sim-observations'


def test_upsert_is_idempotent_put_with_exact_projection():
    transport = RecordingTransport({'json': {'result': 'created'}})
    frame = perception()
    frame_id = asyncio.run(memory(transport).upsert(frame))
    assert frame_id == str(frame.frame_id)
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == 'PUT'
    assert request.url.path == '/annie-sim-observations/_doc/' + str(frame.frame_id)
    assert request.url.scheme == 'https'
    body = jsonlib.loads(request.content)
    assert set(body) == {'frame_id', 'ts', 'map_id', 'pose', 'caption',
                         'person', 'posture', 'location', 'confidence', 'source'}
    assert body['pose'] == {'x': 1.0, 'y': 2.0, 'yaw': 0.5}
    assert body['map_id'] == 'sim-one'
    assert 'image' not in str(body).lower()
    assert b'base64' not in request.content.lower()


def test_rejects_non_simulation_vlm_source_without_request():
    transport = RecordingTransport()
    with pytest.raises(MemoryUnavailable):
        asyncio.run(memory(transport).upsert(perception(source='mock')))
    assert transport.requests == []


def test_search_projects_cited_shape_and_drops_bad_hits():
    good = {'_source': {
        'frame_id': 'f-1', 'ts': 1700, 'map_id': 'sim-one',
        'caption': 'resident sitting in chair',
        'pose': {'x': 1.0, 'y': 2.0, 'yaw': 0.5},
    }}
    transport = RecordingTransport({'json': {'hits': {'hits': [
        good,
        {'_source': {'frame_id': 'f-2', 'ts': 1701, 'map_id': 'sim-other',
                     'caption': 'elsewhere',
                     'pose': {'x': 0, 'y': 0, 'yaw': 0}}},
        {'_source': {'caption': 'incomplete'}},
        {'malformed': True},
        'not-a-dict',
    ]}}})
    result = asyncio.run(memory(transport).search('sitting', 'sim-one'))
    assert len(result) == 1
    assert result[0] == {
        'caption': 'resident sitting in chair',
        'frame_id': 'f-1',
        'ts': 1700,
        'pose': {'x': 1.0, 'y': 2.0, 'yaw': 0.5, 'map_id': 'sim-one'},
    }


def test_search_sends_lexical_query_map_filter_size6():
    transport = RecordingTransport({'json': {'hits': {'hits': []}}})
    asyncio.run(memory(transport).search('sitting', 'sim-one'))
    request = transport.requests[0]
    assert request.method == 'POST'
    assert request.url.path == '/annie-sim-observations/_search'
    body = jsonlib.loads(request.content)
    assert body['size'] == 6
    assert body['query']['bool']['must'] == [{'match': {'caption': 'sitting'}}]
    assert body['query']['bool']['filter'] == [{'term': {'map_id': 'sim-one'}}]
    assert request.headers['Authorization'].startswith('ApiKey ')
    assert 'test-key' not in str(request.url)


def test_search_supports_optional_time_range():
    transport = RecordingTransport({'json': {'hits': {'hits': []}}})
    asyncio.run(memory(transport).search('fall', 'sim-one', ts_from=100, ts_to=200))
    body = jsonlib.loads(transport.requests[0].content)
    assert {'range': {'ts': {'gte': 100, 'lte': 200}}} in body['query']['bool']['filter']


def test_timeout_fails_once_sanitized_without_retry():
    transport = FailingTransport(httpx.ConnectTimeout('timed out'))
    with pytest.raises(MemoryUnavailable) as excinfo:
        asyncio.run(memory(transport).search('anything', 'sim-one'))
    assert transport.calls == 1
    message = str(excinfo.value)
    assert 'test-key' not in message
    assert 'Authorization' not in message
    assert 'timed out' not in message


def test_http_error_is_sanitized():
    transport = FailingTransport(httpx.HTTPStatusError(
        '401',
        request=httpx.Request('PUT', 'https://elastic.example'),
        response=httpx.Response(401)))
    with pytest.raises(MemoryUnavailable) as excinfo:
        asyncio.run(memory(transport).upsert(perception()))
    assert transport.calls == 1
    assert 'test-key' not in str(excinfo.value)


def test_internal_error_text_never_reaches_caller():
    transport = FailingTransport(
        RuntimeError('Authorization: ApiKey test-key leaked'))
    with pytest.raises(MemoryUnavailable) as excinfo:
        asyncio.run(memory(transport).search('x', 'sim-one'))
    assert 'test-key' not in str(excinfo.value)
