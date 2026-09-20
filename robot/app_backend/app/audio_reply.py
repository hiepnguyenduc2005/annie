"""Incident-correlated synthetic audio reply policy.

Two-stage capture protocol for simulated speech: the client registers a
capture before transcribing, then submits the Whisper result. The validator
here is standalone (no FastAPI dependency) so the root owner can reuse it and
wire an atomic service apply method. This module never mutates the service
directly; apply_to_service delegates to the owner-wired method.
"""
import math
from typing import Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import conint, confloat

# Actual model behind robot/simulation/local_stt.py: faster-whisper, tiny.en weights.
STT_MODEL = 'faster-whisper-tiny.en'
MAX_REGISTRY = 100
MAX_CAPTURE_MS = 20000  # Matches the 20 s WAV bound in contract/audio.md.
RESULT_GRACE_MS = 2000  # Result may arrive this long after the reply deadline.


class Segment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: conint(strict=True) | confloat(strict=True, allow_inf_nan=False)
    end: conint(strict=True) | confloat(strict=True, allow_inf_nan=False)
    text: str
    avg_logprob: conint(strict=True) | confloat(strict=True, allow_inf_nan=False)
    no_speech_prob: conint(strict=True) | confloat(strict=True, allow_inf_nan=False)


def _bounded_int(value, name, lo=0, hi=4_102_441_094_000):
    if type(value) is not int:
        raise ValueError(f'{name} must be an integer')
    if not lo <= value <= hi:
        raise ValueError(f'{name} out of bounds')
    return value


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    utterance_id: UUID
    event_id: UUID
    source: Literal['synthetic_replay', 'microphone']
    capture_started_at: int

    @field_validator('capture_started_at')
    @classmethod
    def _check_started(cls, v):
        return _bounded_int(v, 'capture_started_at')


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    utterance_id: UUID
    event_id: UUID
    capture_started_at: int
    capture_ended_at: int
    text: str = Field(min_length=0, max_length=2000)
    source: Literal['synthetic_replay', 'microphone']
    model: Literal['faster-whisper-tiny.en']
    segments: list[Segment] = Field(max_length=100)

    @field_validator('capture_started_at', 'capture_ended_at')
    @classmethod
    def _check_times(cls, v):
        return _bounded_int(v, 'timestamp')


def normalize_text(text):
    """Lowercase, strip punctuation, collapse whitespace for exact matching."""
    # Apostrophes are dropped (im okay == i am okay contract spelling);
    # every other non-letter separates words.
    cleaned = text.lower().replace("'", '').replace('\u2019', '')
    cleaned = ''.join(ch if ch.isalpha() or ch == ' ' else ' ' for ch in cleaned)
    return ' '.join(cleaned.split())


CONCERN = {'help', 'help me', 'i need help', 'not okay', 'im not okay', 'i am not okay',
           'i cant get up', 'okay help me', 'okay help'}
REASSURANCE = {'okay', 'ok', 'im okay', 'i am okay', 'im ok', 'i am ok'}


def classify(text):
    """Exact-intent classifier: concern | reassurance | ambiguous."""
    normalized = normalize_text(text)
    if normalized in CONCERN:
        return 'concern'
    if normalized in REASSURANCE:
        return 'reassurance'
    return 'ambiguous'


def quality_ok(result):
    """Raw Whisper quality rule. No calibrated confidence is fabricated."""
    if not result.segments or not result.text.strip():
        return False
    joined = normalize_text(' '.join(seg.text for seg in result.segments))
    if joined != normalize_text(result.text):
        return False
    return all(seg.avg_logprob >= -1.0 and seg.no_speech_prob <= .75 for seg in result.segments)


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _registry(service):
    if getattr(service, '_audio_reply_registry', None) is None:
        service._audio_reply_registry = {}
    return service._audio_reply_registry


def _evict(registry):
    while len(registry) > MAX_REGISTRY:
        oldest = min(registry, key=lambda eid: registry[eid]['capture_started_at'])
        del registry[oldest]


def register_capture(service, req: CaptureRequest, now):
    """Stage one: register a capture against the active awaiting_reply check-in.

    Returns (ok, reason, entry). One capture per event; registry bounded to
    MAX_REGISTRY events with oldest eviction. Registration declares an
    anticipated end so the root tick can consult pending_grace(service).
    """
    pending = service.pending
    if not pending:
        return False, 'no_active_checkin', None
    if str(req.event_id) != str(pending.get('event_id')):
        return False, 'wrong_incident', None
    if pending.get('phase') != 'awaiting_reply':
        return False, 'phase_not_awaiting_reply', None
    deadline = pending.get('deadline_at')
    if deadline is None:
        return False, 'deadline_not_open', None
    started = pending.get('audio_started_at') or pending.get('started_at')
    if started is not None and req.capture_started_at < started:
        return False, 'question_not_finished', None
    registry = _registry(service)
    if str(req.event_id) in registry:
        return False, 'capture_already_registered', None
    if req.capture_started_at > now:
        return False, 'future_timestamp', None
    if now > deadline:
        return False, 'registered_after_cutoff', None
    entry = {'utterance_id': str(req.utterance_id), 'event_id': str(req.event_id),
             'capture_started_at': req.capture_started_at,
             'anticipated_end_at': req.capture_started_at + MAX_CAPTURE_MS,
             'ended_at': None, 'decision': None}
    registry[str(req.event_id)] = entry
    _evict(registry)
    return True, 'registered', entry


def validate_result(service, req: ResultRequest, now):
    """Standalone stage-two validator. Returns a decision dict.

    Identity is checked before the duplicate shortcut. The only registry
    mutations are marking completion time (ended_at) for valid-bound input
    and storing the decision for the duplicate path; the caller stores the
    final outcome after the atomic apply so a failed apply stays retryable.
    """
    base = {'utterance_id': str(req.utterance_id), 'event_id': str(req.event_id),
            'intent': None, 'eligible': False, 'reason': None, 'applied': False}
    registry = _registry(service)
    entry = registry.get(str(req.event_id))
    if entry is None:
        base['reason'] = 'unknown_capture'
        return base
    # Identity first: a wrong utterance or capture start must never read a
    # stored decision or mutate anything.
    if entry['utterance_id'] != str(req.utterance_id):
        base['reason'] = 'utterance_mismatch'
        return base
    if req.capture_started_at != entry['capture_started_at']:
        base['reason'] = 'capture_mismatch'
        return base
    if entry['decision'] is not None:
        stored = dict(entry['decision'])
        stored['duplicate'] = True
        return stored
    if not all(_finite(seg.avg_logprob) and _finite(seg.no_speech_prob)
               and _finite(seg.start) and _finite(seg.end) for seg in req.segments):
        base['reason'] = 'non_finite_numbers'
        return base
    if req.capture_ended_at < req.capture_started_at:
        base['reason'] = 'ended_before_started'
        return base
    # Raw Whisper timestamps may overshoot the recorded WAV duration
    # (quantization), so only the absolute 0..20 s bound applies; the
    # captured clip duration is not cross-checked.
    if any(seg.start < 0 or seg.end > MAX_CAPTURE_MS / 1000 or seg.end < seg.start
           for seg in req.segments):
        base['reason'] = 'segment_bounds'
        return base
    pending = service.pending
    if not pending:
        base['reason'] = 'no_active_checkin'
        entry['decision'] = base
        return base
    if str(req.event_id) != str(pending.get('event_id')):
        base['reason'] = 'wrong_incident'
        entry['decision'] = base
        return base
    deadline = pending.get('deadline_at')
    if req.capture_ended_at > now:
        base['reason'] = 'future_timestamp'
        entry['decision'] = base
        return base
    if deadline is None or req.capture_ended_at > deadline:
        base['reason'] = 'after_deadline'
        entry['decision'] = base
        return base
    if now > deadline + RESULT_GRACE_MS:
        base['reason'] = 'completion_after_grace'
        entry['decision'] = base
        return base
    intent = classify(req.text)
    base['intent'] = intent
    # Any completed, valid-bound input marks the capture finished, even if
    # quality is rejected: the tick must not wait on it any longer.
    entry['ended_at'] = req.capture_ended_at
    if not quality_ok(req):
        base['reason'] = 'low_quality'
        entry['decision'] = base
        return base
    if intent == 'ambiguous':
        base['reason'] = 'ambiguous_intent'
        entry['decision'] = base
        return base
    base['eligible'] = True
    base['reason'] = 'accepted'
    return base


def apply_to_service(service, decision, now):
    """Explicit quality-accepted apply, delegated to the owner-wired method.

    The root integrates an atomic Service.apply_voice_reply(intent, event_id,
    now); until it exists this raises so callers never silently no-op.
    """
    if not decision.get('eligible'):
        raise ValueError('apply_to_service requires an eligible quality-accepted decision')
    apply_method = getattr(service, 'apply_voice_reply', None)
    if not callable(apply_method):
        raise NotImplementedError('Service.apply_voice_reply is not wired by the owner yet')
    return apply_method(intent=decision['intent'], event_id=decision['event_id'], now=now)


def pending_grace(service):
    """Deadline extension for the root service tick.

    Returns deadline_at + RESULT_GRACE_MS while a registered capture for the
    active check-in is still in flight (no result yet); None otherwise. The
    rule stays strict: only already-registered captures with
    capture_ended_at <= deadline_at can ever count.
    """
    pending = service.pending
    if not pending or pending.get('deadline_at') is None:
        return None
    entry = _registry(service).get(str(pending.get('event_id')))
    if entry and entry['ended_at'] is None and entry['decision'] is None:
        return pending['deadline_at'] + RESULT_GRACE_MS
    return None


def build_audio_reply_router(get_service: Callable, authorize):
    """Minimal router; the app owner includes it with its authorize dependency."""
    router = APIRouter(dependencies=[Depends(authorize)])

    @router.post('/voice/capture')
    async def voice_capture(body: CaptureRequest):
        service = get_service()
        ok, reason, _entry = register_capture(service, body, service.clock())
        if not ok:
            raise HTTPException(422, reason)
        return {'status': 'registered', 'utterance_id': str(body.utterance_id),
                'event_id': str(body.event_id), 'anticipated_end_at': body.capture_started_at + MAX_CAPTURE_MS}

    @router.post('/voice/result')
    async def voice_result(body: ResultRequest):
        service = get_service()
        decision = validate_result(service, body, service.clock())
        if decision['reason'] in ('unknown_capture', 'utterance_mismatch', 'capture_mismatch'):
            raise HTTPException(404, decision['reason'])
        if decision.get('duplicate'):
            # Stored decision replay: return it verbatim, never apply again.
            return decision
        if decision['eligible']:
            applied = apply_to_service(service, decision, service.clock())
            decision['applied'] = bool(applied)
            entry = _registry(service).get(decision['event_id'])
            if entry is not None:
                # Store only the actual outcome; a rejected apply (e.g. the
                # window closed) stays decidable and can never double-fire.
                entry['decision'] = decision
            # apply_to_service raises NotImplementedError when unwired and
            # propagates any other failure, so failures never look applied.
        return decision

    return router
