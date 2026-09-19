import base64
import io
import json
import wave
from uuid import uuid4

import httpx
import pytest

from robot.simulation.resident_reply import ReplyError, replay


def write_wav(path):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x01\0' * 1600)


def rig(tmp_path, *, pending=True, model='tiny.en', identity_ok=True, remaining=8000):
    write_wav(tmp_path / 'okay.wav')
    seen = []
    clock = [100.0]
    event_id = str(uuid4())

    def app_handler(request):
        value = json.loads(request.content) if request.content else None
        seen.append((request.url.path, value, clock[0]))
        if request.url.path == '/status':
            checkin = {'event_id': event_id, 'phase': 'awaiting_reply',
                       'deadline_at': int(clock[0] * 1000) + remaining}
            return httpx.Response(200, json={'pending_checkin': checkin if pending else None})
        if request.url.path == '/voice/result':
            return httpx.Response(200, json={'eligible': True, 'applied': True, 'intent': 'concern'})
        return httpx.Response(200, json={'status': 'registered'})

    def viewer_handler(request):
        value = json.loads(request.content)
        seen.append((request.url.path, value, clock[0]))
        assert set(value) == {'utterance_id', 'audio_b64', 'format', 'source'}
        raw = base64.b64decode(value['audio_b64'], validate=True)
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            assert wav.getnframes() == 1600
        return httpx.Response(200, json={
            'utterance_id': value['utterance_id'] if identity_ok else str(uuid4()),
            'model': model, 'text': 'Not okay.', 'latency_ms': 122.1,
            'segments': [{'start': 0., 'end': 1., 'text': 'Not okay.',
                          'avg_logprob': -.69, 'no_speech_prob': .1}]})

    options = {'app': httpx.Client(base_url='http://app', transport=httpx.MockTransport(app_handler)),
               'viewer': httpx.Client(base_url='http://viewer', transport=httpx.MockTransport(viewer_handler)),
               'directory': tmp_path, 'clock': lambda: clock[0], 'monotonic': lambda: clock[0],
               'sleep': lambda duration: clock.__setitem__(0, clock[0] + duration)}
    return options, seen, event_id


def test_actual_transcript_and_raw_quality_win_over_fixture_name(tmp_path):
    options, seen, event_id = rig(tmp_path)
    result = replay('okay', **options)
    assert [path for path, _, _ in seen] == ['/status', '/voice/capture', '/transcribe-local', '/voice/result']
    sent = seen[-1][1]
    assert sent['text'] == 'Not okay.'
    assert sent['segments'][0]['avg_logprob'] == -.69
    assert 'confidence' not in sent
    assert sent['event_id'] == event_id
    assert sent['source'] == 'synthetic_replay'
    assert sent['capture_ended_at'] - sent['capture_started_at'] == 100
    assert result['decision']['intent'] == 'concern'


@pytest.mark.parametrize('options, message', [
    ({'pending': False}, 'No check-in'),
    ({'remaining': 50}, 'Insufficient response time'),
])
def test_no_capture_without_eligible_window(tmp_path, options, message):
    clients, seen, _ = rig(tmp_path, **options)
    with pytest.raises(ReplyError, match=message):
        replay('okay', **clients)
    assert len(seen) == 1


@pytest.mark.parametrize('options', [{'model': 'wrong'}, {'identity_ok': False}])
def test_recognition_mismatch_never_submits_a_policy_result(tmp_path, options):
    clients, seen, _ = rig(tmp_path, **options)
    with pytest.raises(ReplyError, match='identity or model'):
        replay('okay', **clients)
    assert '/voice/result' not in [path for path, _, _ in seen]


def test_unknown_or_other_incident_never_registers(tmp_path):
    options, seen, _ = rig(tmp_path)
    with pytest.raises(ReplyError, match='Unknown synthetic'):
        replay('../private', **options)
    assert not seen
    with pytest.raises(ReplyError, match='no longer active'):
        replay('okay', event_id=str(uuid4()), **options)
    assert len(seen) == 1
