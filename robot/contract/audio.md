# GX10 brain stand-in audio transcription router

This router adds speech transcription for **synthetic simulation audio** to the
GX10 brain stand-in. It runs on the same service as [brain.md](brain.md), uses
the same provider boundary rules, and has no incident, voice-reply, or
robot-control authority: it returns a transcript and nothing else. Hardware
microphone audio is not accepted; there is no hardware audio input today.

## Integration (owner steps)

The router is independent and does not modify `api.py`:

```python
from app.brain.audio import build_audio_router

app.include_router(build_audio_router(config), dependencies=[Depends(authorize)])
```

`build_audio_router(config)` takes the same `VisionConfig` as the vision app
(mode, base URL, `ANNIE_VISION_API_KEY` credential mapped from
`OPENROUTER_API_KEY` by the launcher, timeout, budget, usage ledger). Audio is
cloud-only: mode `local` or `disabled`, or any non-OpenRouter cloud base URL,
returns 503. The audio model is fixed in code to `xiaomi/mimo-v2.5:floor`;
`ANNIE_VISION_MODEL` is not used for audio.

**Body-size conflict:** the app's streaming body middleware caps requests at
`MAX_BODY_BYTES` (~1.5 MB), sized for 1 MB JPEG frames. Audio allows 4 MB of
decoded WAV, i.e. 5,333,336 base64 characters plus envelope. The owner must
raise the middleware cap for the transcription route to a bounded
5,338,000 bytes (or per-path equivalent); do not remove the streaming bound.
Until then, valid audio requests will fail with 413 at the app boundary.

## Request and response

`POST /transcribe` accepts exactly:

```json
{
  "audio_b64": "<base64 WAV bytes, no data-URL prefix>",
  "format": "wav",
  "source": "simulation_audio",
  "utterance_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81"
}
```

`utterance_id` is a canonical lowercase UUID string preserved from input to
response. Extra keys (including scenario or ground-truth labels) are rejected;
validation errors never echo the audio payload.

WAV bounds, enforced by decoding before provider egress: mono or stereo,
16-bit PCM only, sample rate 8,000-48,000 Hz, duration over one frame and at
most 20 seconds, at most 4,000,000 decoded bytes. The WAV is rewritten into
canonical fmt+data chunks, stripping RIFF metadata (LIST/INFO, cues) before
egress. Audio is never saved by this service.

Successful response (plaintext transcript, no JSON parsing, no confidence):

```json
{
  "text": "I am okay, dear.",
  "utterance_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81",
  "source": "simulation_audio",
  "provider": {"model": "xiaomi/mimo-v2.5:floor"},
  "latency_ms": 640.2
}
```

When reported by the provider, `usage` includes the allowlisted
`prompt_tokens`, `completion_tokens`, `total_tokens`, and `cost_usd`; missing
usage stays absent, never zero. There is no confidence field: a transcript is
unverified provider text, not a calibrated measurement, and policy code must
not treat it as ground truth.

## Provider route

The only approved route is OpenRouter model `xiaomi/mimo-v2.5:floor` using the
official audio input shape
`{"type": "input_audio", "input_audio": {"data": ..., "format": "wav"}}` per
the
[OpenRouter multimodal audio documentation](https://openrouter.ai/docs/guides/overview/multimodal/audio),
with the MiMo-required `data:audio/wav;base64,` prefix on the payload per the
[Xiaomi MiMo audio understanding documentation](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/multimodal-understanding/audio-understanding),
provider price ceilings of $0.15/M prompt and $0.29/M completion tokens,
256 output tokens via `max_tokens`, reasoning disabled, no provider fallback,
and text-only output with no tools. The prompt asks for a verbatim plaintext
transcript only. Verified 2026-09-19 against the synthetic `say`-rendered WAV:
the model returned the spoken sentence verbatim ($0.000037 actual). Bare
base64 without the data-URI prefix is silently treated as missing audio; the
provider then answers with a "no audio attached" refusal, which this service
detects and rejects as 502 rather than surfacing it as a transcript.

Cost bound: the model's 1,050,000-token context at $0.15/M plus 256 output
tokens at $0.29/M is $0.1576; each attempt reserves $0.20 atomically in the
shared budget ledger before egress, including failures and timeouts, under the
same per-service attempt and $20 budget limits as vision. The ledger stays
fail-closed. One attempt per request: no retry, no fallback provider.

Errors contain no transcript: 401 authentication (owner-mounted dependency),
403 origin/client boundary, 413 request bytes, 422 input/WAV validation,
429 busy (one transcription at a time), 502 provider
HTTP/malformed/incomplete/oversized output, 503 disabled or exhausted budget,
504 timeout. Provider errors are sanitized and never become synthetic fallback
transcripts. This endpoint does not create incidents, check-in replies, or
`voice.heard` events; wiring a transcript into policy is a separate,
app-backend decision.

Verification uses synthetic WAVs and mocked HTTP, never paid inference:

```sh
.venv/bin/python -m pytest robot/robot_backend/tests/test_audio.py -q
```
