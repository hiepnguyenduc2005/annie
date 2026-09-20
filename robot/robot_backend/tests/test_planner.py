"""Synthetic JPEG + mocked HTTP planner checks; never invokes a paid provider."""
import base64
import io
import os
import json
import time
from uuid import uuid4

import httpx
from PIL import Image
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    """Point ANNIE_VISION_USAGE_PATH at a temp file for every test.

    Configs that do not pass an explicit usage_path read the real session
    ledger through VisionConfig.from_env-style defaults; tests must never
    reserve against it, even with the HTTP wire mocked.
    """
    monkeypatch.setenv('ANNIE_VISION_USAGE_PATH', str(tmp_path / 'usage.json'))

from robot.robot_backend.app.brain.budget import MODEL_RESERVATION_USD
from robot.robot_backend.app.brain.planner import (
    PLANNER_PROMPT,
    PlanRequest,
    PlanResponse,
    Planner,
    build_planner_router,
)
from robot.robot_backend.app.brain.provider import (
    PRICE_CAPS_USD_PER_M,
    VisionConfig,
)


PLAN_MODEL = 'google/gemini-2.5-flash-lite:floor'

PERCEPTION = {'person': True, 'posture': 'sitting', 'location': 'chair',
              'confidence': 0.83, 'caption': 'A person sits on a chair.'}
ACTION = {'action': 'goto', 'waypoint_id': 'kitchen', 'reason': 'Goal is in the kitchen.'}


def jpeg(size=(64, 48)):
    data = io.BytesIO()
    Image.new('RGB', size, (130, 90, 70)).save(data, format='JPEG')
    return base64.b64encode(data.getvalue()).decode()


def frame(ts=None):
    return {'frame_id': str(uuid4()),
            'ts': ts if ts is not None else int(time.time() * 1000),
            'pose': {'x': 1.25, 'y': -2.5, 'yaw': 0.4, 'map_id': 'sim-map-42'},
            'source': 'simulation_render', 'jpeg_b64': jpeg()}


def plan_request(ts=None, waypoints=None):
    return {'observation': frame(ts),
            'goal': 'Check whether Annie needs anything in the kitchen.',
            'waypoints': waypoints if waypoints is not None
            else [{'id': 'kitchen', 'x': 3.4, 'y': -1.1}],
            'recent_outcomes': [{'command_id': 'cmd-1', 'cmd': 'goto',
                                 'status': 'rejected', 'detail': 'gate refused'}],
            'memories': [{'caption': 'Annie sat on the chair earlier.',
                          'frame_id': str(uuid4()),
                          'ts': int(time.time() * 1000) - 1000,
                          'pose': {'x': 0.0, 'y': 0.0, 'yaw': 0.0,
                                   'map_id': 'sim-map-42'}}]}


def config(**overrides):
    settings = dict(mode='cloud', base_url='https://openrouter.ai/api/v1',
                    model=PLAN_MODEL, api_key='test-secret',
                    usage_path=os.environ.get('ANNIE_VISION_USAGE_PATH',
                                              '/tmp/planner-test-usage.json'))
    settings.update(overrides)
    return VisionConfig(**settings)


def wire_reply(perception=None, action=None):
    content = json.dumps({'perception': PERCEPTION if perception is None else perception,
                          'action': ACTION if action is None else action})
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                  'message': {'content': content}}],
                                     'usage': {'prompt_tokens': 100, 'completion_tokens': 30,
                                               'total_tokens': 130, 'cost': 0.001}})


def client(handler, cfg):
    app = FastAPI()
    app.include_router(build_planner_router(cfg, transport=httpx.MockTransport(handler)))
    return TestClient(app)


def test_plan_sends_real_jpeg_and_returns_correlated_response(tmp_path):
    sent = []
    request = plan_request()

    def handle(req):
        sent.append(json.loads(req.content))
        return wire_reply()

    cfg = config(usage_path=str(tmp_path / 'usage.json'))
    with client(handle, cfg) as api:
        result = api.post('/plan', json=request)
        assert result.status_code == 200, result.text
        data = result.json()
    assert data['perception'] == PERCEPTION
    assert data['action'] == ACTION
    assert data['frame_id'] == request['observation']['frame_id']
    assert data['ts'] == request['observation']['ts']
    assert data['pose'] == request['observation']['pose']
    assert data['provider']['model'] == PLAN_MODEL
    assert data['provider']['usage']['prompt_tokens'] == 100
    assert data['latency_ms'] >= 0
    assert len(sent) == 1
    payload = sent[0]
    image_url = payload['messages'][1]['content'][1]['image_url']['url']
    assert image_url.startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(image_url.split(',')[1]))) as image:
        assert image.size == (64, 48)
    assert payload['max_tokens'] == 512
    assert payload['provider']['max_price'] == PRICE_CAPS_USD_PER_M[PLAN_MODEL]
    assert payload['provider']['allow_fallbacks'] is False
    assert payload['reasoning'] == {'enabled': False}
    assert payload['messages'][0]['content'] == PLANNER_PROMPT
    # Scenario text includes goal/waypoints; pixels carry no metadata.
    user_text = payload['messages'][1]['content'][0]['text']
    assert 'kitchen' in user_text
    # pack_context intentionally includes verified frame pose/time; the pixels
    # and image URL must carry no metadata regardless.
    assert request['observation']['jpeg_b64'] not in json.dumps(payload)


def test_plan_rejects_unknown_waypoint_and_invented_goto(tmp_path):
    path = str(tmp_path / 'usage.json')

    def handle(req):
        return wire_reply(action={'action': 'goto', 'waypoint_id': 'moon-base',
                                  'reason': 'invented'})

    with client(handle, config(usage_path=path)) as api:
        result = api.post('/plan', json=plan_request())
        assert result.status_code == 502
    # Failed egress still consumed its reservation (fail-closed ledger).
    ledger = json.loads(open(path).read())
    assert ledger['models'][PLAN_MODEL]['attempts'] == 1


def test_plan_rejects_stale_and_future_metadata_before_any_wire_call():
    sent = []

    def handle(req):
        sent.append(1)
        return wire_reply()

    now = int(time.time() * 1000)
    with client(handle, config()) as api:
        stale = api.post('/plan', json=plan_request(ts=now - 6000))
        future = api.post('/plan', json=plan_request(ts=now + 4000))
    assert stale.status_code == 422
    assert future.status_code == 422
    assert sent == []


def test_plan_rejects_unknown_fields_and_nonfinite_coords():
    bad_extra = plan_request()
    bad_extra['evil'] = True
    bad_coord = plan_request(waypoints=[{'id': 'x', 'x': float('inf'), 'y': 0.0}])
    for request in (bad_extra, bad_coord):
        with pytest.raises(Exception):
            PlanRequest.model_validate(request)


def test_budget_exhaustion_fails_closed_before_network(tmp_path):
    path = tmp_path / 'usage.json'
    path.parent.mkdir(exist_ok=True)
    reservation = MODEL_RESERVATION_USD[PLAN_MODEL]
    path.write_text(json.dumps({'models': {PLAN_MODEL: {'attempts': 100,
                                                        'reserved_usd': round(100 * reservation, 2)}},
                                'total_reserved_usd': round(100 * reservation, 2)}))
    calls = []

    def handle(req):
        calls.append(1)
        return wire_reply()

    with client(handle, config(usage_path=str(path))) as api:
        result = api.post('/plan', json=plan_request())
    assert result.status_code == 503
    assert calls == []


def test_provider_failure_yields_no_action():
    def handle(req):
        return httpx.Response(500, text='down')

    with client(handle, config()) as api:
        result = api.post('/plan', json=plan_request())
    assert result.status_code == 502


def test_output_size_bounds_enforced():
    from pydantic import ValidationError
    huge_reason = {'action': 'say', 'text': 'hi', 'reason': 'x' * 600}
    response = {'perception': PERCEPTION, 'action': huge_reason,
                'frame_id': str(uuid4()), 'ts': 0,
                'pose': {'x': 0, 'y': 0, 'yaw': 0, 'map_id': 'm'},
                'provider': {'mode': 'cloud', 'model': PLAN_MODEL},
                'latency_ms': 0.0}
    with pytest.raises(ValidationError):
        PlanResponse.model_validate(response)
    # Oversized goal is rejected at request validation.
    oversized = plan_request()
    oversized['goal'] = 'x' * 501
    with pytest.raises(ValidationError):
        PlanRequest.model_validate(oversized)


@pytest.mark.parametrize('trick', ['spin', 'circle', 'zigzag', 'wiggle', 'figure8'])
def test_trick_plan_round_trip_and_ollama_schema(trick):
    from robot.robot_backend.app.brain.planner import LOCAL_PLANNER_PROMPT
    action = {'action': 'trick', 'trick': trick, 'reason': 'Celebrate reassurance.'}
    sent = []
    def handle(req):
        sent.append(json.loads(req.content))
        return wire_reply(action=action)
    with client(handle, config(mode='local', base_url='http://localhost:11434/v1', model='local')) as api:
        result = api.post('/plan', json=plan_request())
    assert result.status_code == 200, result.text
    assert result.json()['action'] == action
    alternatives = sent[0]['response_format']['json_schema']['schema']['properties']['action']['oneOf']
    schema = next(item for item in alternatives if item['properties']['action']['const'] == 'trick')
    assert trick in schema['properties']['trick']['enum']
    assert set(schema['required']) == {'action', 'reason', 'trick'}
    assert 'only for celebrating a reassured resident' in PLANNER_PROMPT
    assert 'only for celebrating a reassured resident' in LOCAL_PLANNER_PROMPT


@pytest.mark.parametrize('action', [
    {'action': 'trick'}, {'action': 'trick', 'trick': 'jump'},
    {'action': 'trick', 'trick': 'spin', 'text': 'hi'},
    {'action': 'trick', 'trick': 'spin', 'waypoint_id': 'home'},
    {'action': 'goto', 'waypoint_id': 'home', 'trick': 'spin'},
    {'action': 'say', 'text': 'hi', 'trick': 'spin'},
    {'action': 'wait', 'trick': 'spin'},
])
def test_trick_conditional_fields_reject_invalid_actions(action):
    from pydantic import ValidationError
    from robot.robot_backend.app.brain.planner import ActionStep
    with pytest.raises(ValidationError):
        ActionStep.model_validate({**action, 'reason': 'test'})
