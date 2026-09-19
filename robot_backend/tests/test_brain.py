"""Synthetic JPEG + mocked HTTP checks; never invokes a paid provider."""
import base64
import io
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from robot_backend.app.brain.api import create_app
from robot_backend.app.brain.models import MAX_BODY_BYTES
from robot_backend.app.brain.budget import APPROVED_VISION_MODELS, MODEL_RESERVATION_USD
from robot_backend.app.brain.provider import (
    COMPACT_PROMPT, PRICE_CAPS_USD_PER_M, PROMPT, VisionConfig, sanitize_jpeg)


OBSERVATION = {'person': True, 'posture': 'lying', 'location': 'floor',
               'confidence': 0.91, 'caption': 'A person lies on the floor.'}

# Primary coverage uses the first approved model; every approved model must
# pass the same envelope and reservation checks.
APPROVED_VISION_MODEL = sorted(APPROVED_VISION_MODELS)[0]


def jpeg(size=(64, 48), format='JPEG'):
    data = io.BytesIO()
    Image.new('RGB', size, (130, 90, 70)).save(data, format=format)
    return base64.b64encode(data.getvalue()).decode()


def frame():
    return {'frame_id': str(uuid4()), 'ts': 1789800000123,
            'pose': {'x': 1.25, 'y': -2.5, 'yaw': 0.4, 'map_id': 'sim-map-42'},
            'source': 'simulation_render', 'jpeg_b64': jpeg()}


def config(mode='cloud'):
    return VisionConfig(mode=mode, base_url='https://vision.example/v1' if mode == 'cloud'
                        else 'http://127.0.0.1:9000/v1', model='test-vision', api_key='test-secret')


def reply(observation=None):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps(OBSERVATION if observation is None else observation)}}]})


def client(handler=lambda request: reply(), *, mode='cloud', token=''):
    return TestClient(create_app(config(mode), token=token, transport=httpx.MockTransport(handler)))


def test_image_inference_preserves_capture_and_sends_pixels_only():
    sent = []
    request = frame()
    def handle(req):
        sent.append(json.loads(req.content))
        assert str(req.url) == 'https://vision.example/v1/chat/completions'
        assert req.headers['authorization'] == 'Bearer test-secret'
        return reply()
    with client(handle) as api:
        result = api.post('/infer', json=request)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['source'] == 'simulation_render'
        assert data['provider'] == {'mode': 'cloud', 'model': 'test-vision'}
        assert data['latency_ms'] >= 0
        perception = data['perception']
        for key in ('ts', 'frame_id', 'pose'):
            assert perception[key] == request[key]
        assert {k: perception[k] for k in OBSERVATION} == OBSERVATION
    assert len(sent) == 1
    assert sent[0]['response_format'] == {'type': 'json_object'}
    assert 'sim-map-42' not in json.dumps(sent)
    assert request['frame_id'] not in json.dumps(sent)
    image_url = sent[0]['messages'][1]['content'][1]['image_url']['url']
    assert image_url.startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(image_url.split(',')[1]))) as image:
        assert image.size == (64, 48)


def test_local_inference_compact_settings_and_resize():
    """Local mode: 320 px resize, compact prompt, 128 tokens, temp 0."""
    sent = []
    big = jpeg((640, 480))
    request = frame() | {'jpeg_b64': big}
    def handle(req):
        sent.append(json.loads(req.content))
        return reply()
    with client(handle, mode='local') as api:
        result = api.post('/infer', json=request)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['perception']['frame_id'] == request['frame_id']
        assert data['perception']['ts'] == request['ts']
    assert len(sent) == 1
    assert sent[0]['max_tokens'] == 128
    assert sent[0]['temperature'] == 0
    system = sent[0]['messages'][0]['content']
    assert system == COMPACT_PROMPT and system != PROMPT
    image_url = sent[0]['messages'][1]['content'][1]['image_url']['url']
    with Image.open(io.BytesIO(base64.b64decode(image_url.split(',')[1]))) as image:
        assert max(image.size) == 320
        assert image.size[0] / image.size[1] == 4 / 3
    # Wire payload carries pixels only: no frame identity leaks via the image.
    wire = json.dumps(sent)
    assert request['frame_id'] not in wire
    assert str(request['ts']) not in wire


def test_cloud_payload_is_unchanged_by_local_tuning():
    sent = []
    big = jpeg((640, 480))
    def handle(req):
        sent.append(json.loads(req.content))
        return reply()
    with client(handle) as api:
        assert api.post('/infer', json=frame() | {'jpeg_b64': big}).status_code == 200
    assert 'temperature' not in sent[0]
    # Generic cloud keeps the 512-token completion cap and no temperature key.
    assert sent[0]['max_completion_tokens'] == 512
    assert sent[0]['messages'][0]['content'] == PROMPT
    image_url = sent[0]['messages'][1]['content'][1]['image_url']['url']
    with Image.open(io.BytesIO(base64.b64decode(image_url.split(',')[1]))) as image:
        assert image.size == (640, 480)


@pytest.mark.parametrize('model', sorted(APPROVED_VISION_MODELS))
def test_openrouter_reserves_before_failed_egress_and_blocks_after_restart(tmp_path, model):
    path = tmp_path / 'usage.json'
    cfg = VisionConfig(mode='cloud', base_url='https://openrouter.ai/api/v1',
                       model=model, api_key='test',
                       max_cloud_calls=1, usage_path=str(path))
    calls = []
    def fail(request):
        assert json.loads(path.read_text())['models'][model]['attempts'] == 1
        payload = json.loads(request.content)
        assert payload['provider']['max_price'] == PRICE_CAPS_USD_PER_M[model]
        assert payload['provider']['allow_fallbacks'] is False
        assert payload['max_tokens'] == 512
        assert payload['reasoning'] == {'enabled': False}
        assert payload['modalities'] == ['text']
        calls.append(request)
        raise httpx.ReadTimeout('ambiguous charge')
    with TestClient(create_app(cfg, token='', transport=httpx.MockTransport(fail))) as api:
        assert api.post('/infer', json=frame()).status_code == 504
    with TestClient(create_app(cfg, token='', transport=httpx.MockTransport(fail))) as api:
        assert api.post('/infer', json=frame()).status_code == 503
    assert len(calls) == 1
    state = json.loads(path.read_text())
    assert state['models'][model] == {'attempts': 1,
                                      'reserved_usd': MODEL_RESERVATION_USD[model]}
    assert state['total_reserved_usd'] == pytest.approx(MODEL_RESERVATION_USD[model])


def test_shared_ledger_accumulates_across_models(tmp_path):
    from robot_backend.app.brain.budget import BudgetError, reserve_attempt
    path = tmp_path / 'usage.json'
    reserve_attempt(str(path), 100, 20, model='qwen/qwen3-vl-32b-instruct:floor',
                    reservation_usd=MODEL_RESERVATION_USD['qwen/qwen3-vl-32b-instruct:floor'])
    reserve_attempt(str(path), 100, 20, model='deepseek/deepseek-v4.1-flash:floor',
                    reservation_usd=MODEL_RESERVATION_USD['deepseek/deepseek-v4.1-flash:floor'])
    state = json.loads(path.read_text())
    assert state['total_reserved_usd'] == pytest.approx(0.52)
    assert state['models']['qwen/qwen3-vl-32b-instruct:floor']['attempts'] == 1
    assert state['models']['deepseek/deepseek-v4.1-flash:floor']['attempts'] == 1
    # The cap binds the combined total across models, not each model:
    # adding any model exceeds a budget equal to the exact reserved total.
    with pytest.raises(BudgetError):
        reserve_attempt(str(path), 100, 0.52, model='xiaomi/mimo-v2.5:floor',
                        reservation_usd=MODEL_RESERVATION_USD['xiaomi/mimo-v2.5:floor'])
    with pytest.raises(BudgetError):
        reserve_attempt(str(path), 100, 0.52, model='qwen/qwen3-vl-32b-instruct:floor',
                        reservation_usd=MODEL_RESERVATION_USD['qwen/qwen3-vl-32b-instruct:floor'])


def test_legacy_single_model_ledger_is_migrated(tmp_path):
    from robot_backend.app.brain.budget import reserve_attempt
    path = tmp_path / 'usage.json'
    path.write_text(json.dumps({'model': 'qwen/qwen3-vl-32b-instruct:floor',
                                'attempts': 2, 'reserved_usd': 0.04}))
    reserve_attempt(str(path), 100, 20, model='qwen/qwen3-vl-32b-instruct:floor',
                    reservation_usd=MODEL_RESERVATION_USD['qwen/qwen3-vl-32b-instruct:floor'])
    state = json.loads(path.read_text())
    assert state['models']['qwen/qwen3-vl-32b-instruct:floor']['attempts'] == 3
    assert state['total_reserved_usd'] == pytest.approx(0.06)


@pytest.mark.parametrize('change', [
    {'ts': '123'}, {'ts': True}, {'frame_id': 'not-uuid'}, {'frame_id': 123},
    {'extra': 'forbidden'}, {'source': 'mock'}, {'scenario': 'fall'},
    {'pose': {'x': '1', 'y': 0, 'yaw': 0, 'map_id': 'test'}},
    {'pose': {'x': 1, 'y': 0, 'yaw': 0, 'map_id': 'test', 'extra': True}},
])
def test_strict_frame_schema(change):
    with client(lambda _: pytest.fail('invalid input reached provider')) as api:
        request = frame() | change
        response = api.post('/infer', json=request)
        assert response.status_code == 422
        assert request['jpeg_b64'] not in response.text


@pytest.mark.parametrize('encoded', ['not-base64', base64.b64encode(b'not jpeg').decode(),
                                      jpeg(format='PNG'), jpeg((1281, 1))])
def test_invalid_images_rejected(encoded):
    with client(lambda _: pytest.fail('invalid image reached provider')) as api:
        response = api.post('/infer', json=frame() | {'jpeg_b64': encoded})
        assert response.status_code == 422


def test_sanitize_resize_preserves_pixels_and_rejects_bad_targets():
    big = jpeg((640, 480))
    resized = sanitize_jpeg(big, 320)
    with Image.open(io.BytesIO(base64.b64decode(resized))) as image:
        assert image.size == (320, 240)
    # No resize target keeps the legacy full-size path.
    with Image.open(io.BytesIO(base64.b64decode(sanitize_jpeg(big)))) as image:
        assert image.size == (640, 480)
    for bad in (0, 32, -320, 1281, '320', 320.0):
        with pytest.raises(ValueError):
            sanitize_jpeg(big, bad)


def test_cloud_rejects_hardware_before_provider():
    with client(lambda _: pytest.fail('hardware escaped cloud boundary')) as api:
        assert api.post('/infer', json=frame() | {'source': 'hardware'}).status_code == 403


def test_local_accepts_hardware():
    with client(mode='local') as api:
        response = api.post('/infer', json=frame() | {'source': 'hardware'})
        assert response.status_code == 200
        assert response.json()['source'] == 'hardware'


@pytest.mark.parametrize('kwargs', [
    {'mode': 'local', 'base_url': 'http://example.com/v1'},
    {'mode': 'local', 'base_url': 'https://127.0.0.1/v1'},
    {'mode': 'local', 'base_url': 'http://127.0.0.1.evil.test/v1'},
    {'mode': 'cloud', 'base_url': 'http://example.com/v1'},
    {'mode': 'cloud', 'base_url': 'https://user:pass@example.com/v1'},
    {'mode': 'cloud', 'base_url': 'https://example.com/v1?key=secret'},
    {'mode': 'cloud', 'api_key': ''}, {'model': ''}, {'timeout_s': 0},
])
def test_unsafe_or_incomplete_config_rejected(kwargs):
    values = {'mode': 'cloud', 'base_url': 'https://vision.example/v1', 'model': 'test', 'api_key': 'secret'}
    with pytest.raises(ValueError):
        VisionConfig(**(values | kwargs))


def test_localhost_is_pinned_to_loopback_ip():
    cfg = VisionConfig(mode='local', base_url='http://localhost:9000/v1', model='test')
    assert cfg.endpoint == 'http://127.0.0.1:9000/v1/chat/completions'


def test_openai_fallback_is_host_scoped(monkeypatch):
    monkeypatch.setenv('ANNIE_VISION_MODE', 'cloud')
    monkeypatch.setenv('ANNIE_VISION_MODEL', 'test')
    monkeypatch.setenv('ANNIE_VISION_API_KEY', '')
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-only-secret')
    monkeypatch.setenv('ANNIE_VISION_BASE_URL', 'https://api.openai.com/v1')
    assert VisionConfig.from_env().api_key == 'openai-only-secret'
    monkeypatch.setenv('ANNIE_VISION_BASE_URL', 'https://other.example/v1')
    with pytest.raises(ValueError):
        VisionConfig.from_env()


def test_disabled_has_no_egress_and_safe_config():
    with TestClient(create_app(VisionConfig(), token='')) as api:
        assert api.post('/infer', json=frame()).status_code == 503
        assert api.get('/health').json()['configured'] is False
    with client() as api:
        info = api.get('/health/config')
        assert info.status_code == 200
        assert 'test-secret' not in info.text
        assert 'vision.example' not in info.text


def test_timeout_is_explicit():
    def timeout(_request):
        raise httpx.ReadTimeout('SECRET RAW PROVIDER DETAILS')
    with client(timeout) as api:
        response = api.post('/infer', json=frame())
        assert response.status_code == 504
        assert 'SECRET' not in response.text
        assert 'perception' not in response.json()


@pytest.mark.parametrize('change', [
    {'person': 'true'}, {'confidence': '0.9'}, {'confidence': 1.1},
    {'posture': 'fallen'}, {'location': 'sofa'}, {'groundtruth': 'fall'},
    {'caption': 'x' * 2001}, {'person': 1},
])
def test_provider_observation_is_strict(change):
    with client(lambda _: reply(OBSERVATION | change)) as api:
        response = api.post('/infer', json=frame())
        assert response.status_code == 502
        assert 'perception' not in response.json()


@pytest.mark.parametrize('response', [
    httpx.Response(200, content='not json SECRET'),
    httpx.Response(200, json={'choices': []}),
    httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}),
    httpx.Response(200, content=b'x' * 65537),
    httpx.Response(429, content='SECRET provider debug'),
    httpx.Response(302, headers={'location': 'https://evil.example/'}),
])
def test_provider_failures_never_create_observations(response):
    count = []
    def handle(request):
        count.append(request)
        return response
    with client(handle) as api:
        result = api.post('/infer', json=frame())
        assert result.status_code == 502
        assert 'SECRET' not in result.text
        assert len(count) == 1


def test_auth_host_origin_and_body_bounds():
    with client(token='local-token') as api:
        assert api.post('/infer', json=frame()).status_code == 401
        assert api.get('/health/config').status_code == 401
        assert api.get('/health', headers={'Authorization': 'Bearer local-token'}).status_code == 200
    with client() as api:
        assert api.get('/health', headers={'Host': 'attacker.example'}).status_code == 400
        assert api.get('/health', headers={'Origin': 'https://attacker.example'}).status_code == 403
        assert api.post('/infer', content=b'x' * (MAX_BODY_BYTES + 1)).status_code == 413
        assert api.post('/infer', content=(b'x' * 800000 for _ in range(2))).status_code == 413
    with TestClient(create_app(config(), token=''), client=('192.168.1.9', 1234)) as remote:
        assert remote.get('/health').status_code == 403


def test_provider_usage_allowlist():
    def handle(_request):
        response = reply().json()
        response['usage'] = {'prompt_tokens': 900, 'completion_tokens': 90,
                             'total_tokens': 990, 'cost': 0.001, 'raw_private': 'SECRET'}
        return httpx.Response(200, json=response)
    with client(handle) as api:
        response = api.post('/infer', json=frame())
        assert response.status_code == 200
        assert response.json()['provider']['usage'] == {
            'prompt_tokens': 900, 'completion_tokens': 90, 'total_tokens': 990, 'cost_usd': 0.001}
        assert 'SECRET' not in response.text


def test_corrupted_ledger_fails_closed(tmp_path):
    from robot_backend.app.brain.budget import reserve_attempt, BudgetError
    path = tmp_path / 'usage.json'
    path.write_text('bad json')
    with pytest.raises(BudgetError):
        reserve_attempt(str(path), 100, 20, model=APPROVED_VISION_MODEL,
                        reservation_usd=MODEL_RESERVATION_USD[APPROVED_VISION_MODEL])


def test_budget_reservation_cap(tmp_path):
    from robot_backend.app.brain.budget import reserve_attempt, BudgetError
    path = tmp_path / 'usage.json'
    reservation = MODEL_RESERVATION_USD[APPROVED_VISION_MODEL]
    reserve_attempt(str(path), 100, reservation, model=APPROVED_VISION_MODEL,
                    reservation_usd=reservation)
    with pytest.raises(BudgetError):
        reserve_attempt(str(path), 100, reservation, model=APPROVED_VISION_MODEL,
                        reservation_usd=reservation)
