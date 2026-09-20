"""Incident-correlated synthetic audio reply policy tests."""
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from robot.app_backend.app.audio_reply import (MAX_REGISTRY, RESULT_GRACE_MS, STT_MODEL,
                             build_audio_reply_router, classify,
                             normalize_text, pending_grace,
                             register_capture, validate_result)


class FakeService:
    """Matches the documented Service surface used by this policy only."""

    def __init__(self, clock):
        self.clock = clock
        self.pending = None
        self.applied = []

    def apply_voice_reply(self, intent, event_id, now):
        self.applied.append((intent, event_id, now))
        self.pending = None
        return {'applied': True}


@pytest.fixture
def clock():
    return {'now': 100000}


@pytest.fixture
def svc(clock):
    return FakeService(lambda: clock['now'])


def open_window(svc, now=100000, event_id=None):
    svc.pending = {'event_id': str(event_id or uuid4()), 'phase': 'awaiting_reply',
                   'audio_started_at': now - 100, 'deadline_at': now + 8000}
    return svc.pending['event_id']


def capture_req(event_id, started=100000, utterance=None):
    return {'utterance_id': str(utterance or uuid4()), 'event_id': str(event_id),
            'source': 'synthetic_replay', 'capture_started_at': started}


def result_req(event_id, started=100000, ended=105000, utterance=None,
               text='okay', model=STT_MODEL, segments=None):
    if segments is None:
        segments = [{'start': 0.0, 'end': 1.0, 'text': text,
                     'avg_logprob': -0.4, 'no_speech_prob': 0.1}]
    return {'utterance_id': str(utterance or uuid4()), 'event_id': str(event_id),
            'capture_started_at': started, 'capture_ended_at': ended,
            'text': text, 'source': 'synthetic_replay', 'model': model,
            'segments': segments}


def register_ok(svc, event_id, started=100000):
    from robot.app_backend.app.audio_reply import CaptureRequest
    utterance = uuid4()
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(event_id, started, utterance)), svc.clock())
    assert ok, reason
    svc._utt = utterance
    return utterance


def validate_ok(svc, event_id, **overrides):
    from robot.app_backend.app.audio_reply import ResultRequest
    overrides.setdefault('utterance', getattr(svc, '_utt', None))
    ended = overrides.get('ended', 105000)
    base_clock = svc.clock
    # The result can only arrive at wall time >= capture end.
    svc.clock = lambda: max(base_clock(), ended)
    req = result_req(event_id, **overrides)
    return validate_result(svc, ResultRequest(**req), svc.clock())


# ---------------------------------------------------------------- intents

@pytest.mark.parametrize('text,expected', [
    ('help', 'concern'), ('Help!', 'concern'), ('help me', 'concern'),
    ('I need help', 'concern'), ('not okay', 'concern'), ("I'm not okay", 'concern'),
    ("I can't get up", 'concern'), ('okay help me', 'concern'), ('okay help', 'concern'),
    ('okay', 'reassurance'), ('OK.', 'reassurance'), ("I'm okay", 'reassurance'),
    ('I am okay', 'reassurance'),
    ('okay or help', 'ambiguous'), ('the weather is nice', 'ambiguous'),
    ('', 'ambiguous'), ('   ', 'ambiguous'),
])
def test_classify_exact_intents(text, expected):
    assert classify(text) == expected


def test_normalize_strips_punctuation_and_case():
    assert normalize_text("I'm OKAY!!  ") == 'im okay'


# ------------------------------------------------------------- registration

def test_register_requires_awaiting_reply_phase(svc):
    event_id = open_window(svc)
    svc.pending['phase'] = 'awaiting_playback'
    from robot.app_backend.app.audio_reply import CaptureRequest
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(event_id)), svc.clock())
    assert not ok and reason == 'phase_not_awaiting_reply'


def test_register_rejects_wrong_incident(svc):
    open_window(svc)
    from robot.app_backend.app.audio_reply import CaptureRequest
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(uuid4())), svc.clock())
    assert not ok and reason == 'wrong_incident'


def test_register_rejects_no_active_checkin(svc):
    from robot.app_backend.app.audio_reply import CaptureRequest
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(uuid4())), svc.clock())
    assert not ok and reason == 'no_active_checkin'


def test_register_rejects_future_and_after_cutoff(svc):
    event_id = open_window(svc, now=100000)
    from robot.app_backend.app.audio_reply import CaptureRequest
    svc.clock = lambda: 100000
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(event_id, 100001)), svc.clock())
    assert not ok and reason == 'future_timestamp'
    svc.clock = lambda: 109500  # Past deadline_at = 108000, still real time.
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(event_id, 109000)), svc.clock())
    assert not ok and reason == 'registered_after_cutoff'


def test_one_capture_per_event(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    from robot.app_backend.app.audio_reply import CaptureRequest
    ok, reason, _ = register_capture(svc, CaptureRequest(**capture_req(event_id)), svc.clock())
    assert not ok and reason == 'capture_already_registered'


def test_registry_bounded_oldest_evicted(clock):
    svc = FakeService(lambda: clock['now'])
    for _ in range(MAX_REGISTRY + 5):
        event_id = open_window(svc, now=clock['now'])
        register_ok(svc, event_id, started=clock['now'])
        clock['now'] += 1
        svc.pending = {'event_id': str(uuid4()), 'phase': 'awaiting_reply',
                       'audio_started_at': clock['now'], 'deadline_at': clock['now'] + 8000}
    assert len(svc._audio_reply_registry) == MAX_REGISTRY


# ------------------------------------------------------------------ result

def test_valid_reassurance_accepted_and_applied(svc, clock):
    event_id = open_window(svc, now=clock['now'])
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, ended=105000)
    assert decision['eligible'] and decision['intent'] == 'reassurance'


def test_low_quality_rejected_raw_signals_only(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, segments=[
        {'start': 0.0, 'end': 1.0, 'text': 'okay', 'avg_logprob': -1.5, 'no_speech_prob': 0.1}])
    assert not decision['eligible'] and decision['reason'] == 'low_quality'
    decision = validate_ok(svc, event_id, segments=[
        {'start': 0.0, 'end': 1.0, 'text': 'okay', 'avg_logprob': -0.4, 'no_speech_prob': 0.9}])
    assert decision.get('duplicate') and decision['reason'] == 'low_quality'


def test_ambiguous_and_silence_rejected(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, text='okay or help')
    assert not decision['eligible'] and decision['reason'] == 'ambiguous_intent'
    # Silence is a separate capture on a fresh check-in; the first capture
    # already stored its one decision.
    event_id = open_window(svc, now=200000)
    svc.clock = lambda: 205000
    utterance = register_ok(svc, event_id, started=200000)
    from robot.app_backend.app.audio_reply import ResultRequest
    req = result_req(event_id, started=200000, ended=205000, utterance=utterance,
                     text='', segments=[])
    decision = validate_result(svc, ResultRequest(**req), svc.clock())
    assert not decision['eligible'] and decision['reason'] == 'low_quality'


def test_segments_must_match_text(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, text='okay', segments=[
        {'start': 0.0, 'end': 1.0, 'text': 'help me', 'avg_logprob': -0.4, 'no_speech_prob': 0.1}])
    assert not decision['eligible'] and decision['reason'] == 'low_quality'


def test_non_finite_rejected(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    # Strict Pydantic types reject non-finite or wrongly-typed numbers at
    # the API boundary, before any policy evaluation (422 at the route).
    import pydantic
    from robot.app_backend.app.audio_reply import ResultRequest
    bad_segments = [
        {'start': 0.0, 'end': 1.0, 'text': 'okay', 'avg_logprob': float('nan'), 'no_speech_prob': 0.1},
        {'start': float('-inf'), 'end': 1.0, 'text': 'okay', 'avg_logprob': -0.4, 'no_speech_prob': 0.1},
    ]
    for segments in bad_segments:
        with pytest.raises(pydantic.ValidationError):
            ResultRequest(**result_req(event_id, utterance=svc._utt, segments=segments))
    with pytest.raises(pydantic.ValidationError):
        ResultRequest(**{**result_req(event_id, utterance=svc._utt), 'capture_started_at': 100000.5})


def test_segment_bounds_zero_to_twenty_seconds(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, segments=[
        {'start': 0.0, 'end': 20.5, 'text': 'okay', 'avg_logprob': -0.4, 'no_speech_prob': 0.1}])
    assert decision['reason'] == 'segment_bounds'
    decision = validate_ok(svc, event_id, segments=[
        {'start': -0.1, 'end': 1.0, 'text': 'okay', 'avg_logprob': -0.4, 'no_speech_prob': 0.1}])
    assert decision['reason'] == 'segment_bounds'


def test_whisper_timestamp_overshoot_allowed(svc):
    # Actual tiny.en behavior: a 0.4 s WAV yields segment 0..1.0.
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, segments=[
        {'start': 0.0, 'end': 1.0, 'text': 'okay', 'avg_logprob': -0.4, 'no_speech_prob': 0.1}])
    assert decision['eligible'] and decision['reason'] == 'accepted'


# ------------------------------------------------- real Service integration

from robot.app_backend.app.service import Service as RealService


def _real_svc(clock, **kwargs):
    svc = RealService(':memory:', clock=clock, **kwargs)
    return svc


def _suspect(svc, clock):
    """Two risky frames 1 s apart start a check-in; receipt mode adds playback."""
    for _ in range(2):
        clock['now'] += 1000
        svc.ingest('brain.perception', {'ts': clock['now'], 'frame_id': str(uuid4()),
                                        'person': True, 'posture': 'lying', 'location': 'floor',
                                        'confidence': .95, 'caption': 'down', 'pose': {'x': 1, 'y': 2}})
    assert svc.pending


def _complete_say(svc):
    command = next(item for item in svc.commands if item['cmd'] == 'say')
    svc.command_receipt(command['command_id'], 'accepted', 'simulation')
    svc.command_receipt(command['command_id'], 'executing', 'simulation')
    svc.command_receipt(command['command_id'], 'completed', 'simulation')


def _roundtrip(svc, clock, text, avg_logprob=-0.4):
    """Register + result against the real service; returns the decision."""
    from robot.app_backend.app.audio_reply import CaptureRequest, ResultRequest
    from robot.app_backend.app.audio_reply import apply_to_service
    event_id = svc.pending['event_id']
    started = clock['now']
    utterance = uuid4()
    ok, reason, _ = register_capture(svc, CaptureRequest(
        utterance_id=utterance, event_id=event_id, source='synthetic_replay',
        capture_started_at=started), svc.clock())
    assert ok, reason
    clock['now'] = started + 5000
    req = ResultRequest(utterance_id=utterance, event_id=event_id,
                        capture_started_at=started, capture_ended_at=started + 700,
                        text=text, source='synthetic_replay', model=STT_MODEL,
                        segments=[{'start': 0.0, 'end': 1.0, 'text': text,
                                   'avg_logprob': avg_logprob, 'no_speech_prob': 0.1}])
    decision = validate_result(svc, req, svc.clock())
    if decision['eligible']:
        decision['applied'] = bool(apply_to_service(svc, decision, svc.clock()))
        entry = svc._audio_reply_registry.get(decision['event_id'])
        entry['decision'] = decision
    return decision


@pytest.mark.parametrize('text,intent,kind', [
    ('okay', 'reassurance', 'checkin_ok'),
    ('help', 'concern', 'fall_confirmed'),
])
def test_real_service_apply_exactly_once(clock, text, intent, kind):
    svc = _real_svc(lambda: clock['now'], require_audio_receipt=True)
    _suspect(svc, clock)
    _complete_say(svc)
    event_id = svc.pending['event_id']
    decision = _roundtrip(svc, clock, text)
    assert decision['eligible'] and decision['intent'] == intent
    assert [e['kind'] for e in svc.events()].count(kind) == 1
    assert svc.pending is None
    svc.close()
    svc.close()


def test_real_service_grace_tick_only_for_registered_inflight(clock):
    from robot.app_backend.app.audio_reply import pending_grace
    # No registered input: deadline expiry escalates with input_unavailable.
    svc = _real_svc(lambda: clock['now'], require_audio_receipt=True)
    _suspect(svc, clock)
    _complete_say(svc)
    deadline = svc.pending['deadline_at']
    svc.tick(deadline + 1)
    events = [e for e in svc.events() if e['kind'] == 'checkin_audio_failed']
    assert events and events[-1]['reason'] == 'input_unavailable'
    svc.close()

    # Registered in-flight capture: past deadline + grace the tick escalates
    # with recognition_timeout, and just after deadline (inside grace) it waits.
    clock['now'] += 60000
    svc = _real_svc(lambda: clock['now'], require_audio_receipt=True)
    _suspect(svc, clock)
    _complete_say(svc)
    deadline = svc.pending['deadline_at']
    from robot.app_backend.app.audio_reply import CaptureRequest
    event_id = svc.pending['event_id']
    ok, _, _ = register_capture(svc, CaptureRequest(
        utterance_id=uuid4(), event_id=event_id, source='synthetic_replay',
        capture_started_at=clock['now']), svc.clock())
    assert ok
    assert pending_grace(svc) == deadline + RESULT_GRACE_MS
    svc.tick(deadline + 1000)  # Inside the grace window.
    assert svc.pending is not None
    svc.tick(deadline + RESULT_GRACE_MS + 1)  # Grace exhausted.
    events = [e for e in svc.events() if e['kind'] == 'checkin_audio_failed']
    assert events and events[-1]['reason'] == 'recognition_timeout'
    svc.close()


def test_apply_raise_then_retry_succeeds(clock):

    svc = _real_svc(lambda: clock['now'], require_audio_receipt=True)

    _suspect(svc, clock)

    _complete_say(svc)

    from robot.app_backend.app.audio_reply import apply_to_service

    original = svc.apply_voice_reply

    state = {'fail': True}

    def flaky(intent, event_id, now):

        if state['fail']:

            raise RuntimeError('transient')

        return original(intent, event_id, now)

    svc.apply_voice_reply = flaky

    event_id = svc.pending['event_id']

    started = clock['now']

    utterance = uuid4()

    from robot.app_backend.app.audio_reply import CaptureRequest, ResultRequest

    ok, reason, _ = register_capture(svc, CaptureRequest(

        utterance_id=utterance, event_id=event_id, source='synthetic_replay',

        capture_started_at=started), svc.clock())

    assert ok, reason

    clock['now'] = started + 5000

    req = ResultRequest(utterance_id=utterance, event_id=event_id,

                        capture_started_at=started, capture_ended_at=started + 700,

                        text='okay', source='synthetic_replay', model=STT_MODEL,

                        segments=[{'start': 0.0, 'end': 1.0, 'text': 'okay',

                                   'avg_logprob': -0.4, 'no_speech_prob': 0.1}])

    decision = validate_result(svc, req, svc.clock())

    assert decision['eligible']

    with pytest.raises(RuntimeError):

        apply_to_service(svc, decision, svc.clock())

    # The registry entry stays decidable: not marked applied, not terminal.

    entry = svc._audio_reply_registry[event_id]

    assert entry['decision'] is None

    state['fail'] = False

    assert apply_to_service(svc, decision, svc.clock()) is True

    entry['decision'] = {**decision, 'applied': True}

    assert [e['kind'] for e in svc.events()].count('checkin_ok') == 1

    svc.close()





def test_duplicate_wrong_capture_start_rejected(clock):

    svc = _real_svc(lambda: clock['now'], require_audio_receipt=True)

    _suspect(svc, clock)

    _complete_say(svc)

    event_id = svc.pending['event_id']

    decision = _roundtrip(svc, clock, 'okay')

    assert decision['eligible'] and decision['applied']

    from robot.app_backend.app.audio_reply import ResultRequest

    utterance = uuid4()

    entry = svc._audio_reply_registry.get(event_id)

    assert entry is not None and entry['decision'] is not None

    req = ResultRequest(utterance_id=entry['utterance_id'], event_id=event_id,

                        capture_started_at=entry['capture_started_at'] + 1,

                        capture_ended_at=clock['now'], text='okay',

                        source='synthetic_replay', model=STT_MODEL,

                        segments=[{'start': 0.0, 'end': 1.0, 'text': 'okay',

                                   'avg_logprob': -0.4, 'no_speech_prob': 0.1}])

    decision = validate_result(svc, req, svc.clock())

    # Identity failure outranks the duplicate shortcut.

    assert decision['reason'] == 'capture_mismatch'

    svc.close()





def test_duplicate_successful_result_no_second_apply(clock):
    svc = _real_svc(lambda: clock['now'], require_audio_receipt=True)
    _suspect(svc, clock)
    _complete_say(svc)
    event_id = svc.pending['event_id']
    decision = _roundtrip(svc, clock, 'okay')
    assert decision['eligible'] and decision['applied']
    assert [e['kind'] for e in svc.events()].count('checkin_ok') == 1
    # After the check-in cleared, a new capture for the same incident is
    # rejected and no second checkin_ok can be produced.
    from robot.app_backend.app.audio_reply import CaptureRequest
    ok, reason, _ = register_capture(svc, CaptureRequest(
        utterance_id=uuid4(), event_id=event_id, source='synthetic_replay',
        capture_started_at=clock['now']), svc.clock())
    assert not ok and reason in ('capture_already_registered', 'no_active_checkin')
    assert [e['kind'] for e in svc.events()].count('checkin_ok') == 1
    svc.close()


def test_wrong_incident_and_unknown_capture(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, utterance=uuid4())
    assert decision['reason'] == 'utterance_mismatch'
    decision = validate_ok(svc, uuid4())
    assert decision['reason'] == 'unknown_capture'


def test_stale_result_after_checkin_cleared(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    svc.pending = None
    decision = validate_ok(svc, event_id)
    assert not decision['eligible'] and decision['reason'] == 'no_active_checkin'


def test_duplicate_utterance_one_effect(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    first = validate_ok(svc, event_id)
    second = validate_ok(svc, event_id)
    # The validator only returns the accepted decision once; after the caller
    # stores it (post-apply), a replay returns the stored outcome verbatim.
    assert first['eligible']
    entry = svc._audio_reply_registry[event_id]
    entry['decision'] = {**first, 'applied': True}
    second = validate_ok(svc, event_id)
    assert second['eligible'] and second.get('duplicate') and second['applied']


def test_completion_after_deadline_rejected(svc):
    event_id = open_window(svc, now=100000)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, ended=109000)
    assert not decision['eligible'] and decision['reason'] == 'after_deadline'


def test_capture_wrong_start_or_end_rejected(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    # Wrong capture start is an identity failure (checked first).
    decision = validate_ok(svc, event_id, started=106000, ended=107000)
    assert decision['reason'] == 'capture_mismatch'
    # A registered start with a pre-start end fails the ordering rule.
    decision = validate_ok(svc, event_id, started=100000, ended=99000)
    assert decision['reason'] == 'ended_before_started'


def test_future_capture_end_rejected(svc, clock):
    event_id = open_window(svc, now=clock['now'])
    register_ok(svc, event_id)
    from robot.app_backend.app.audio_reply import ResultRequest
    req = result_req(event_id, ended=clock['now'] + 1, utterance=svc._utt)
    decision = validate_result(svc, ResultRequest(**req), clock['now'])
    assert decision['reason'] == 'future_timestamp'


def test_wrong_capture_start_rejected(svc):
    event_id = open_window(svc)
    register_ok(svc, event_id)
    decision = validate_ok(svc, event_id, started=100001)
    assert decision['reason'] == 'capture_mismatch'


# --------------------------------------------------------- pending grace

def test_pending_grace_only_inflight_registered(svc):
    assert pending_grace(svc) is None
    event_id = open_window(svc, now=100000)
    assert pending_grace(svc) is None
    register_ok(svc, event_id)
    assert pending_grace(svc) == svc.pending['deadline_at'] + RESULT_GRACE_MS
    validate_ok(svc, event_id, ended=105000)
    assert pending_grace(svc) is None


# ------------------------------------------------------------------ router

def _app(svc):
    app = FastAPI()
    app.include_router(build_audio_reply_router(lambda: svc, lambda: None))
    return TestClient(app)


def test_router_capture_and_result_roundtrip(svc, clock):
    client = _app(svc)
    event_id = open_window(svc)
    utterance = uuid4()
    r = client.post('/voice/capture', json=capture_req(event_id, utterance=utterance))
    assert r.status_code == 200 and r.json()['status'] == 'registered'
    clock['now'] = 105000
    r = client.post('/voice/result', json=result_req(event_id, utterance=utterance))
    assert r.status_code == 200
    body = r.json()
    assert body['eligible'] and body['intent'] == 'reassurance' and body['applied']
    assert svc.applied == [('reassurance', str(event_id), svc.clock())]


def test_router_rejects_unknown_model_and_source():
    client = _app(FakeService(lambda: 1))
    r = client.post('/voice/result', json=result_req(uuid4(), model='whisper-large'))
    assert r.status_code == 422
    r = client.post('/voice/capture', json={**capture_req(uuid4()), 'source': 'hardware_mic'})
    assert r.status_code == 422


def test_router_capture_rejection_422(svc):
    client = _app(svc)
    r = client.post('/voice/capture', json=capture_req(uuid4()))
    assert r.status_code == 422
    assert 'no_active_checkin' in r.json()['detail']


def test_microphone_source_is_accepted_for_capture_and_result():
    from robot.app_backend.app.audio_reply import CaptureRequest, ResultRequest
    from uuid import uuid4
    ids = {'utterance_id': str(uuid4()), 'event_id': str(uuid4())}
    CaptureRequest.model_validate({**ids, 'source': 'microphone', 'capture_started_at': 1000})
    ResultRequest.model_validate({**ids, 'source': 'microphone', 'model': 'faster-whisper-tiny.en',
                                  'capture_started_at': 1000, 'capture_ended_at': 2000, 'text': '', 'segments': []})
    import pytest
    with pytest.raises(Exception):
        CaptureRequest.model_validate({**ids, 'source': 'phone_call', 'capture_started_at': 1000})
