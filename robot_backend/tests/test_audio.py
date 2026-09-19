"""Synthetic WAV + mocked HTTP checks; never invokes a paid provider."""
import asyncio
import base64
import io
import json
import tempfile
import wave
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from robot_backend.app.brain import audio
from robot_backend.app.brain.audio import (
    AUDIO_MODEL, AUDIO_RESERVATION_USD, MAX_BASE64_CHARS, build_audio_router)
from robot_backend.app.brain.budget import BudgetError
from robot_backend.app.brain.provider import VisionConfig


def wav_bytes(rate=16000, channels=1, width=2, seconds=1.0, frames=None, magic=b'RIFF'):
    data = io.BytesIO()
    with wave.open(data, 'wb') as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(width)
        clip.setframerate(rate)
        count = int(rate * seconds) if frames is None else frames
        clip.writeframes(b'\x00' * (count * channels * width))
    blob = data.getvalue()
    if magic != b'RIFF':
        blob = magic + blob[4:]
    return blob


def wav_b64(**kwargs):
    return base64.b64encode(wav_bytes(**kwargs)).decode()


def utterance(**kwargs):
    return {'audio_b64': wav_b64(), 'format': 'wav',
            'source': 'simulation_audio', 'utterance_id': str(uuid4())} | kwargs


def config(mode='cloud', usage_path=None):
    return VisionConfig(mode=mode,
                        base_url='https://openrouter.ai/api/v1' if mode == 'cloud'
                        else 'http://127.0.0.1:9000/v1',
                        model='xiaomi/mimo-v2.5:floor', api_key='test-secret',
                        usage_path=usage_path or tempfile.mkdtemp() + '/usage.json')


def reply(text='Hello grandma', usage=None):
    body = {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}
    if usage:
        body['usage'] = usage
    return httpx.Response(200, json=body)


def client(handler=lambda request: reply(), *, mode='cloud'):
    app = FastAPI()
    app.include_router(build_audio_router(config(mode), transport=httpx.MockTransport(handler)))
    return TestClient(app)


def test_transcription_roundtrip_preserves_utterance_and_sends_audio_only():
    sent = []
    request = utterance()
    def handle(req):
        sent.append(json.loads(req.content))
        assert str(req.url) == 'https://openrouter.ai/api/v1/chat/completions'
        assert req.headers['authorization'] == 'Bearer test-secret'
        return reply(usage={'prompt_tokens': 3000, 'completion_tokens': 6,
                            'total_tokens': 3006, 'cost': 0.0005, 'raw_private': 'SECRET'})
    with client(handle) as api:
        result = api.post('/transcribe', json=request)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['text'] == 'Hello grandma'
        assert data['utterance_id'] == request['utterance_id']
        assert data['source'] == 'simulation_audio'
        assert data['provider'] == {'model': AUDIO_MODEL}
        assert data['latency_ms'] >= 0
        assert data['usage'] == {'prompt_tokens': 3000, 'completion_tokens': 6,
                                 'total_tokens': 3006, 'cost_usd': 0.0005}
        assert 'SECRET' not in result.text
    assert len(sent) == 1
    payload = sent[0]
    assert payload['model'] == AUDIO_MODEL
    assert payload['max_tokens'] == 256
    assert 'max_completion_tokens' not in payload
    assert payload['reasoning'] == {'enabled': False}
    assert payload['modalities'] == ['text']
    assert payload['provider'] == {'max_price': {'prompt': 0.15, 'completion': 0.29},
                                   'allow_fallbacks': False, 'require_parameters': True}
    message = payload['messages'][0]
    assert message['role'] == 'user'
    part = message['content'][1]
    assert part['type'] == 'input_audio'
    assert part['input_audio']['format'] == 'wav'
    data = part['input_audio']['data']
    assert data.startswith('data:audio/wav;base64,')
    decoded = base64.b64decode(data.split(',', 1)[1], validate=True)
    assert decoded[:4] == b'RIFF'
    # Utterance ID and capture metadata never reach the provider.
    assert request['utterance_id'] not in json.dumps(sent)


def test_cloud_audio_can_be_explicitly_enabled_with_local_vision():
    from robot_backend.app.brain.api import create_app
    requests = []
    def handle(request):
        requests.append(request)
        return reply()
    app = create_app(config('local'), token='', audio_config=config('cloud'),
                     transport=httpx.MockTransport(handle))
    with TestClient(app) as api:
        health = api.get('/health').json()
        assert health['mode'] == 'local'
        assert health['audio_model'] == AUDIO_MODEL
        assert api.post('/transcribe', json=utterance()).status_code == 200
    assert len(requests) == 1
    assert requests[0].url.host == 'openrouter.ai'


def test_wav_is_rewritten_without_metadata_chunks():
    sent = []
    noisy = io.BytesIO()
    noisy.write(b'RIFF')
    body = io.BytesIO()
    with wave.open(body, 'wb') as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(b'\x00' * 3200)
    inner = body.getvalue()[12:]
    info_payload = b'INFOISFT' + (6).to_bytes(4, 'little') + b'SECRET'
    info = b'LIST' + len(info_payload).to_bytes(4, 'little') + info_payload
    blob = b'RIFF' + (4 + len(info) + len(inner)).to_bytes(4, 'little') + b'WAVE' + info + inner
    with client(lambda req: sent.append(json.loads(req.content)) or reply()) as api:
        result = api.post('/transcribe', json=utterance(
            audio_b64=base64.b64encode(blob).decode()))
        assert result.status_code == 200, result.text
    sent_audio = base64.b64decode(
        sent[0]['messages'][0]['content'][1]['input_audio']['data'].split(',', 1)[1])
    assert b'LIST' not in sent_audio and b'SECRET' not in sent_audio
    with wave.open(io.BytesIO(sent_audio), 'rb') as clean:
        assert clean.getframerate() == 16000


@pytest.mark.parametrize('change', [
    {'format': 'mp3'}, {'source': 'hardware_mic'}, {'source': 'microphone'},
    {'utterance_id': 'not-uuid'}, {'utterance_id': str(uuid4()).upper()},
    {'utterance_id': 123}, {'extra': 'forbidden'}, {'audio_b64': ''},
    {'audio_b64': 'x' * (MAX_BASE64_CHARS + 4)},
])
def test_strict_transcription_schema(change):
    request = utterance(**change)
    with client(lambda _: pytest.fail('invalid input reached provider')) as api:
        response = api.post('/transcribe', json=request)
        assert response.status_code == 422
        if request.get('audio_b64'):
            assert request['audio_b64'] not in response.text
        assert 'SECRET' not in response.text


@pytest.mark.parametrize('encoded', [
    'not-base64',
    base64.b64encode(b'not wav').decode(),
    base64.b64encode(wav_bytes(magic=b'RIFX')).decode(),
    wav_b64(width=1),
    wav_b64(width=4),
    wav_b64(channels=4),
    wav_b64(rate=7999),
    wav_b64(rate=48001),
    wav_b64(seconds=21.0),
    wav_b64(frames=0),
    base64.b64encode(wav_bytes()[:-17]).decode(),  # truncated data chunk
])
def test_invalid_wav_rejected_before_provider(encoded):
    with client(lambda _: pytest.fail('invalid audio reached provider')) as api:
        response = api.post('/transcribe', json=utterance(audio_b64=encoded))
        assert response.status_code == 422
        assert encoded not in response.text


def test_oversized_decoded_wav_rejected():
    oversized = wav_b64(rate=48000, channels=2, seconds=20.0)  # 3,840,044 decoded
    with client() as api:
        assert api.post('/transcribe', json=utterance(audio_b64=oversized)).status_code == 200
    raw = wav_bytes(rate=48000, channels=2, seconds=20.0)
    junk = 4_000_002 - len(raw) - 8
    raw += b'JUNK' + junk.to_bytes(4, 'little') + b'\x00' * junk  # 4,000,002 decoded
    too_big = base64.b64encode(raw).decode()
    with client(lambda _: pytest.fail('oversized audio reached provider')) as api:
        response = api.post('/transcribe', json=utterance(audio_b64=too_big))
        assert response.status_code == 422
        assert 'exceeds 4000000 decoded bytes' in response.text


@pytest.mark.parametrize('mode', ['disabled', 'local'])
def test_non_cloud_audio_is_disabled(mode):
    with client(lambda _: pytest.fail('disabled mode reached provider'), mode=mode) as api:
        assert api.post('/transcribe', json=utterance()).status_code == 503


def test_non_openrouter_cloud_is_refused():
    cfg = VisionConfig(mode='cloud', base_url='https://audio.example/v1',
                       model='xiaomi/mimo-v2.5:floor', api_key='test-secret')
    app = FastAPI()
    app.include_router(build_audio_router(cfg))
    with TestClient(app) as api:
        assert api.post('/transcribe', json=utterance()).status_code == 503


def test_budget_failure_is_503_before_egress(monkeypatch):
    def exhausted(path, max_calls, budget_usd, **kwargs):
        raise BudgetError('Audio cloud reservation limit reached')
    monkeypatch.setattr(audio, 'reserve_attempt', exhausted)
    with client(lambda _: pytest.fail('exhausted budget reached provider')) as api:
        response = api.post('/transcribe', json=utterance())
        assert response.status_code == 503
        assert 'SECRET' not in response.text


def test_reservation_uses_audio_model_and_upper_bound():
    seen = []
    def record(path, max_calls, budget_usd, **kwargs):
        seen.append((path, max_calls, budget_usd, kwargs))
    original = audio.reserve_attempt
    audio.reserve_attempt = record
    try:
        with client() as api:
            assert api.post('/transcribe', json=utterance()).status_code == 200
    finally:
        audio.reserve_attempt = original
    assert len(seen) == 1
    assert seen[0][3] == {'model': AUDIO_MODEL, 'reservation_usd': AUDIO_RESERVATION_USD}
    # 1,050,000 context tokens * $0.15/M + 256 * $0.29/M = $0.1576 < $0.20.
    assert 1_050_000 * 0.15 / 1_000_000 + 256 * 0.29 / 1_000_000 < AUDIO_RESERVATION_USD


def test_real_ledger_records_audio_reservation(tmp_path):
    usage = str(tmp_path / 'usage.json')
    app = FastAPI()
    app.include_router(build_audio_router(
        config(usage_path=usage), transport=httpx.MockTransport(lambda _: reply())))
    with TestClient(app) as api:
        assert api.post('/transcribe', json=utterance()).status_code == 200
        assert api.post('/transcribe', json=utterance()).status_code == 200
    state = json.loads(open(usage).read())
    assert state == {'models': {AUDIO_MODEL: {'attempts': 2, 'reserved_usd': 0.4}},
                     'total_reserved_usd': 0.4}


def test_real_ledger_budget_exhaustion_fails_closed(tmp_path):
    usage = str(tmp_path / 'usage.json')
    cfg = VisionConfig(mode='cloud', base_url='https://openrouter.ai/api/v1',
                       model='xiaomi/mimo-v2.5:floor', api_key='test-secret',
                       usage_path=usage, max_cloud_calls=1)
    app = FastAPI()
    app.include_router(build_audio_router(
        cfg, transport=httpx.MockTransport(lambda _: reply())))
    with TestClient(app) as api:
        assert api.post('/transcribe', json=utterance()).status_code == 200
        response = api.post('/transcribe', json=utterance())
        assert response.status_code == 503
        assert response.json()['detail'] == 'Cloud reservation limit reached'


def test_timeout_is_explicit_and_sanitized():
    def timeout(_request):
        raise httpx.ReadTimeout('SECRET RAW PROVIDER DETAILS')
    with client(timeout) as api:
        response = api.post('/transcribe', json=utterance())
        assert response.status_code == 504
        assert 'SECRET' not in response.text
        assert 'text' not in response.json()


@pytest.mark.parametrize('response', [
    httpx.Response(200, content='not json SECRET'),
    httpx.Response(200, json={'choices': []}),
    httpx.Response(200, json={'choices': [{'finish_reason': 'length',
                                           'message': {'content': 'cut off'}}]}),
    httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                           'message': {'content': ['not', 'text']}}]}),
    httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                           'message': {'content': 'x' * 4001}}]}),
    httpx.Response(200, content=b'x' * 65537),
    httpx.Response(429, content='SECRET provider debug'),
    httpx.Response(302, headers={'location': 'https://evil.example/'}),
])
def test_provider_failures_never_create_transcripts(response):
    count = []
    def handle(request):
        count.append(request)
        return response
    with client(handle) as api:
        result = api.post('/transcribe', json=utterance())
        assert result.status_code == 502
        assert 'SECRET' not in result.text
        assert len(count) == 1  # single attempt, no retry or fallback


def test_usage_allowlist_omits_unknown_fields():
    def handle(_request):
        return reply(usage={'prompt_tokens': 100, 'raw_private': 'SECRET'})
    with client(handle) as api:
        result = api.post('/transcribe', json=utterance())
        assert result.status_code == 200
        assert result.json()['usage'] == {'prompt_tokens': 100}
        assert 'SECRET' not in result.text


def test_missing_usage_stays_absent():
    with client(lambda _: reply()) as api:
        result = api.post('/transcribe', json=utterance())
        assert result.status_code == 200
        assert 'usage' not in result.json()


@pytest.mark.parametrize('refusal', [
    "I don't see an audio file attached to your message. Could you please "
    "upload or share the audio you'd like me to transcribe?",
    'I cannot hear any audio in this conversation.',
    'No audio was provided, so there is nothing to transcribe.',
    'Please upload the audio file and I will transcribe it.',
    'No Audio File Attached: please provide the audio.',
])
def test_attachment_refusal_never_becomes_transcript(refusal):
    with client(lambda _: reply(refusal)) as api:
        result = api.post('/transcribe', json=utterance())
        assert result.status_code == 502
        assert result.json()['detail'] == 'Audio provider received no audio attachment'
        assert refusal not in result.text


def test_legitimate_transcript_with_audio_word_passes():
    with client(lambda _: reply('The audio said: grandma is okay.')) as api:
        result = api.post('/transcribe', json=utterance())
        assert result.status_code == 200
        assert result.json()['text'] == 'The audio said: grandma is okay.'


def test_concurrent_requests_are_bounded():
    active = []
    async def slow():
        loop = asyncio.get_event_loop()
        def handle(_request):
            active.append(1)
            assert len(active) == 1
            import time as clock
            clock.sleep(0.1)
            active.pop()
            return reply()
        with client(handle) as api:
            first = loop.run_in_executor(None, lambda: api.post('/transcribe', json=utterance()))
            second = loop.run_in_executor(None, lambda: api.post('/transcribe', json=utterance()))
            results = await asyncio.gather(first, second)
            assert sorted(r.status_code for r in results) == [200, 429]
    asyncio.run(slow())


def test_empty_transcription_is_explicit():
    with client(lambda _: reply('   ')) as api:
        result = api.post('/transcribe', json=utterance())
        assert result.status_code == 200
        assert result.json()['text'] == ''
