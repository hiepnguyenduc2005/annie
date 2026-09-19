"""Bounded, non-retrying OpenRouter audio transcription router.

Owner integration: create this router from the same VisionConfig as the vision
service and mount it on the brain app with the existing authentication
dependency, e.g. app.include_router(build_audio_router(config),
dependencies=[Depends(authorize)]). The endpoint never creates incidents,
replies, or robot commands; it only transcribes synthetic simulation audio.
"""
import asyncio
import base64
import binascii
from dataclasses import dataclass
import io
import json
import time
from typing import Literal
from uuid import UUID
import wave

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, ValidationError, field_validator
import httpx

from .budget import APPROVED_AUDIO_MODELS, MODEL_RESERVATION_USD, BudgetError, reserve_attempt
from .models import ProviderUsage, StrictModel
from .provider import VisionConfig

# The only approved audio route: exact OpenRouter floor model with verified
# $0.15/M prompt and $0.29/M completion price ceilings. There is no local or
# fallback provider for audio.
AUDIO_MODEL = next(iter(APPROVED_AUDIO_MODELS))
AUDIO_RESERVATION_USD = MODEL_RESERVATION_USD[AUDIO_MODEL]
# Upper bound proof: 1,050,000 context tokens * $0.15/M + 256 output tokens
# * $0.29/M = $0.1576 < $0.20. The reservation remains spent after failures.
MAX_COMPLETION_TOKENS = 256
PROMPT_PRICE_USD_PER_M = 0.15
COMPLETION_PRICE_USD_PER_M = 0.29

MAX_WAV_BYTES = 4_000_000
MAX_BASE64_CHARS = 4 * ((MAX_WAV_BYTES + 2) // 3)
MAX_DURATION_S = 20.0
MIN_SAMPLE_RATE_HZ = 8_000
MAX_SAMPLE_RATE_HZ = 48_000
MAX_TEXT_CHARS = 4_000
MAX_RESPONSE_BYTES = 65_536

# Provider replies that describe a missing attachment are routing failures,
# not transcripts; they must never reach policy as spoken words.
REFUSAL_MARKERS = (
    'no audio file',
    'no audio attached',
    "don't see an audio",
    'not see an audio',
    'cannot see an audio',
    "can't see an audio",
    'cannot access the audio',
    "can't access the audio",
    'cannot hear',
    "can't hear",
    'cannot listen',
    "can't listen",
    'unable to access the audio',
    'unable to hear',
    'unable to listen',
    'unable to process the audio',
    'upload the audio',
    'attach the audio',
    'share the audio',
    'provide the audio file',
    'no audio was provided',
    'audio file was not provided',
    'without the audio',
    'no attached audio',
    'missing audio',
)

PROMPT = (
    'Transcribe this audio. Return only the spoken words as plain text, '
    'without commentary, timestamps, or speaker labels. If the audio contains '
    'no intelligible speech, return an empty response.'
)


class InvalidAudio(ValueError):
    pass


class AudioProviderError(RuntimeError):
    pass


class AudioProviderTimeout(AudioProviderError):
    pass


class TranscribeRequest(StrictModel):
    audio_b64: str = Field(min_length=4, max_length=MAX_BASE64_CHARS, repr=False)
    format: Literal['wav']
    source: Literal['simulation_audio']
    utterance_id: UUID

    @field_validator('utterance_id', mode='before')
    @classmethod
    def canonical_uuid(cls, value):
        if not isinstance(value, str):
            raise ValueError('utterance_id must be a canonical UUID string')
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError('utterance_id must be a canonical UUID string')
        return parsed


class AudioProviderMetadata(StrictModel):
    model: str


class TranscriptionResult(StrictModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    utterance_id: UUID
    source: Literal['simulation_audio']
    provider: AudioProviderMetadata
    latency_ms: float = Field(ge=0)
    usage: ProviderUsage | None = None


def sanitize_wav(encoded: str) -> str:
    """Validate PCM WAV bounds and rewrite canonical fmt+data chunks.

    Rewriting strips RIFF metadata chunks (LIST/INFO, cues) so only audio
    samples leave the trusted network. Returns canonical base64 WAV.
    """
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise InvalidAudio('Invalid base64 WAV encoding') from None
    if len(raw) > MAX_WAV_BYTES:
        raise InvalidAudio('WAV exceeds 4000000 decoded bytes')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as clip:
            channels = clip.getnchannels()
            width = clip.getsampwidth()
            rate = clip.getframerate()
            frames = clip.getnframes()
            if channels not in (1, 2):
                raise InvalidAudio('Expected mono or stereo WAV')
            if width != 2 or clip.getcomptype() != 'NONE':
                raise InvalidAudio('Expected 16-bit PCM WAV')
            if not MIN_SAMPLE_RATE_HZ <= rate <= MAX_SAMPLE_RATE_HZ:
                raise InvalidAudio('Sample rate must be between 8000 and 48000 Hz')
            if frames < 1 or frames / rate > MAX_DURATION_S:
                raise InvalidAudio('WAV duration must be between one frame and 20 seconds')
            samples = clip.readframes(frames)
            if len(samples) != frames * channels * width:
                raise InvalidAudio('Truncated WAV data chunk')
    except InvalidAudio:
        raise
    except (wave.Error, EOFError, OSError, ValueError):
        raise InvalidAudio('Invalid WAV encoding') from None
    canonical = io.BytesIO()
    try:
        with wave.open(canonical, 'wb') as clean:
            clean.setnchannels(channels)
            clean.setsampwidth(width)
            clean.setframerate(rate)
            clean.writeframes(samples)
    except (wave.Error, OSError):
        raise InvalidAudio('Invalid WAV encoding') from None
    if canonical.tell() > MAX_WAV_BYTES:
        raise InvalidAudio('WAV exceeds 4000000 decoded bytes')
    return base64.b64encode(canonical.getvalue()).decode('ascii')


@dataclass(frozen=True)
class AudioProviderReply:
    text: str
    usage: ProviderUsage | None = None


async def transcribe_audio(config: VisionConfig, wav_b64: str, *, transport=None) -> AudioProviderReply:
    """One bounded, non-retrying OpenRouter audio request; plaintext out."""
    payload = {'model': AUDIO_MODEL, 'messages': [
        {'role': 'user', 'content': [
            {'type': 'text', 'text': PROMPT},
            # Xiaomi MiMo requires a data-URI prefix on base64 audio; bare
            # base64 is silently treated as if no audio was attached.
            {'type': 'input_audio', 'input_audio': {
                'data': 'data:audio/wav;base64,' + wav_b64, 'format': 'wav'}},
        ]}],
        'max_tokens': MAX_COMPLETION_TOKENS,
        # Reasoning disabled: mimo does not support max_completion_tokens and
        # reasoning tokens would otherwise exhaust the bounded output budget.
        'reasoning': {'enabled': False},
        'provider': {'max_price': {'prompt': PROMPT_PRICE_USD_PER_M,
                                   'completion': COMPLETION_PRICE_USD_PER_M},
                     'allow_fallbacks': False, 'require_parameters': True},
        'modalities': ['text']}
    # Persist the reservation before egress; failed/ambiguous calls keep it.
    await asyncio.to_thread(reserve_attempt, config.usage_path,
                            config.max_cloud_calls, config.budget_usd,
                            model=AUDIO_MODEL, reservation_usd=AUDIO_RESERVATION_USD)
    headers = {'Authorization': 'Bearer ' + config.api_key}
    try:
        # Total timeout also bounds slow trickles; neither proxy env nor
        # redirects may route resident audio or the credential elsewhere.
        async with asyncio.timeout(config.timeout_s):
            async with httpx.AsyncClient(timeout=config.timeout_s, follow_redirects=False,
                                         trust_env=False, transport=transport) as client:
                async with client.stream('POST', config.endpoint, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        raise AudioProviderError('Audio provider request failed')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise AudioProviderError('Audio provider response exceeds limit')
        envelope = json.loads(data)
        choice = envelope['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise AudioProviderError('Audio provider did not complete a transcription')
        content = choice['message']['content']
        if not isinstance(content, str) or len(content) > MAX_TEXT_CHARS:
            raise AudioProviderError('Audio provider returned an invalid transcription')
        if any(marker in content.lower() for marker in REFUSAL_MARKERS):
            raise AudioProviderError('Audio provider received no audio attachment')
        usage = envelope.get('usage')
        reported = None
        if isinstance(usage, dict):
            # Copy an allowlist only, never arbitrary provider metadata. Invalid
            # accounting must not be mistaken for zero cost by callers.
            values = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                      if key in usage}
            if 'cost' in usage:
                values['cost_usd'] = usage['cost']
            reported = ProviderUsage.model_validate(values) if values else None
        return AudioProviderReply(text=content.strip(), usage=reported)
    except (TimeoutError, httpx.TimeoutException):
        raise AudioProviderTimeout('Audio provider timed out') from None
    except AudioProviderError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, ValidationError):
        raise AudioProviderError('Audio provider returned an invalid response') from None


def build_audio_router(config: VisionConfig, *, transport=None) -> APIRouter:
    """Independent transcription router; owner mounts it with authorize."""
    router = APIRouter()
    lock = asyncio.Lock()

    @router.post('/transcribe', response_model=TranscriptionResult,
                 response_model_exclude_none=True)
    async def transcribe(request: Request):
        if config.mode != 'cloud' or not config.is_openrouter:
            raise HTTPException(503, 'Audio transcription is disabled')
        if lock.locked():
            raise HTTPException(429, 'Audio transcription is already running')
        try:
            payload = TranscribeRequest.model_validate(await request.json())
        except (ValidationError, ValueError, UnicodeDecodeError):
            # Never echo request values; audio payloads must not appear in errors.
            raise HTTPException(422, 'Invalid transcription request; consult contract/audio.md') from None
        started = time.perf_counter()
        try:
            async with lock:
                wav_b64 = await asyncio.to_thread(sanitize_wav, payload.audio_b64)
                reply = await transcribe_audio(config, wav_b64, transport=transport)
        except InvalidAudio as exc:
            raise HTTPException(422, str(exc)) from None
        except BudgetError as exc:
            raise HTTPException(503, str(exc)) from None
        except AudioProviderTimeout as exc:
            raise HTTPException(504, str(exc)) from None
        except AudioProviderError as exc:
            raise HTTPException(502, str(exc)) from None
        return TranscriptionResult(text=reply.text, utterance_id=payload.utterance_id,
                                   source=payload.source,
                                   provider=AudioProviderMetadata(model=AUDIO_MODEL),
                                   latency_ms=round((time.perf_counter() - started) * 1000, 3),
                                   usage=reply.usage)

    return router
