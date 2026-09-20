# Annie companion backend

FastAPI companion using Deepgram for speech recognition and speech synthesis,
with local `Qwen/Qwen2.5-Omni-3B` for text reasoning. `/generate` accepts text,
audio, or both. Images are rejected with 422; a separate image API is deferred.
The simulator under `../robot/` is independent.

```text
Audio → Deepgram Nova-3 → transcript ┐
Typed text ─────────────────────────┴→ Qwen text reply → Deepgram Aura-2 → WAV
```

Deepgram receives current uploaded audio for transcription and only the spoken
reply for synthesis. Private session memory and summaries are not sent as TTS
input. The dedicated server still receives only high-level final results.
No fallback to Qwen speech is used. Both Deepgram calls use the existing `httpx`
dependency; no new package installation is needed.

## Start

On the GB10, activate the existing **`qwen-env` GPU environment**:

```sh
vllm serve Qwen/Qwen2.5-Omni-3B --omni --host 127.0.0.1 --port 8091
```

In another terminal, from the repository root (Python 3.10+):

```sh
cd robot_backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
```

Set `DEEPGRAM_API_KEY` in `robot_backend/.env` before starting (never in an
example file). Defaults are `DEEPGRAM_STT_MODEL=nova-3`,
`DEEPGRAM_TTS_MODEL=aura-2-thalia-en`, `DEEPGRAM_LANGUAGE=en`, and a 30-second
speech timeout. Change the TTS model to choose another Deepgram voice. Configure
`QWEN_BASE_URL` for the reachable Qwen server; Qwen remains required.

The adapter follows Deepgram's [transcription API](https://developers.deepgram.com/docs/pre-recorded-audio)
and [WAV synthesis API](https://developers.deepgram.com/docs/tts-container).
WAV output is mono linear16 at 24 kHz and stays base64 in the phone response.

Edit `.env` to set the exact dedicated-server POST endpoint and credentials. `PHONE_API_KEY` authenticates `/generate` and `/sessions/*/end`;
`SERVER_API_KEY` authenticates `/requests`. Empty inbound keys disable that
actor's authentication for local development; set both for LAN use. `/health`
reports wrapper liveness, not GPU readiness. OpenAPI: `http://127.0.0.1:8080/docs`.
Environment variables override `.env`; its location is the working directory.

Use **one worker**: sessions, per-session locks and remote-request deduplication
are in memory. Active sessions do not survive process restarts. A shared store
must implement the `SessionManager` unit-of-work interface and distributed locks
before using multiple workers. Do not use `--reload` for resident sessions.

### vLLM compatibility evidence

All Qwen calls use `/v1/chat/completions` with `modalities: ["text"]`.
Qwen receives typed text and temporary Deepgram transcriptions, never uploaded
images or audio. Internal JSON is validated locally. Speech synthesis is handled
only by Deepgram; the existing Qwen launch command can remain unchanged.

The supplied deployment versions are:

- vLLM-Omni `0.29.0rc2.dev187+g573ec4cda`
- source revision `v0.29.0rc1-187-g573ec4cd`
- resolved commit `573ec4cdace9dceafff8ee9f6aa11b207a832938`
- vLLM `0.29.0`

The matching [serving implementation](https://github.com/vllm-project/vllm-omni/blob/573ec4cdace9dceafff8ee9f6aa11b207a832938/vllm_omni/entrypoints/openai/serving_chat.py),
[Qwen example](https://github.com/vllm-project/vllm-omni/blob/573ec4cdace9dceafff8ee9f6aa11b207a832938/examples/online_serving/qwen2_5_omni/gradio_demo.py),
and [launch documentation](https://github.com/vllm-project/vllm-omni/blob/573ec4cdace9dceafff8ee9f6aa11b207a832938/docs/user_guide/examples/online_serving/qwen2_5_omni.md)
were inspected and support text-only requests. The wrapper does not install or
upgrade vLLM. Actual GB10 inference, voice quality and playback need a live check;
normal automated tests mock both providers without paid calls.

## API examples

These commands use `jq` to extract session IDs. Set the shell variables to match
the inbound keys configured in `.env` (leave empty for local development):

```sh
BASE=http://127.0.0.1:8080
PHONE_API_KEY=
SERVER_API_KEY=

# Health
curl --fail-with-body "$BASE/health"

# Grandma starts with audio; response has session_id, base64 audio and done.
curl --fail-with-body "$BASE/generate" \
  -H "Authorization: Bearer $PHONE_API_KEY" \
  -F 'audio=@sample.wav;type=audio/wav'

# Text request
curl --fail-with-body "$BASE/generate" \
  -H "Authorization: Bearer $PHONE_API_KEY" \
  -F 'text=Hello puppy, can we chat about my day?'

# Dedicated server creates a task; retries with identical request_id/body dedup.
SESSION_ID=$(curl --fail-with-body "$BASE/requests" \
  -H "Authorization: Bearer $SERVER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req_456","type":"check_in","request":"Check whether Grandma ate breakfast."}' \
  | jq -r .session_id)

# The phone obtains the opening question. start=true is allowed once per task.
curl --fail-with-body "$BASE/generate" \
  -H "Authorization: Bearer $PHONE_API_KEY" \
  -F "session_id=$SESSION_ID" -F start=true

# Continue the same session with the resident's reply.
curl --fail-with-body "$BASE/generate" \
  -H "Authorization: Bearer $PHONE_API_KEY" \
  -F "session_id=$SESSION_ID" -F 'text=Yes, I had breakfast. Thank you.'

# Explicit finalization; safe to repeat, including after natural completion.
curl --fail-with-body -X POST "$BASE/sessions/$SESSION_ID/end" \
  -H "Authorization: Bearer $PHONE_API_KEY"
```

Use `-H 'Content-Type: application/json' -d '{"reason":"cancelled"}'` on `/end`
to cancel. Phone responses never include the spoken text, private memory or final
summary. Audio is a base64 WAV synthesized by Deepgram; decode and play it. Generated audio is not proof of audible playback.

`/requests` acknowledges a task; it does not deliver a push notification to the
phone. The caller must pass `session_id` to its phone/dog transport, which calls
`/generate` with `start=true`. There is no invented iPhone integration or playback
receipt. Supplying resident input directly on that session also starts it.

## Session and privacy behavior

- The local session holds a goal, UTC creation/update timestamps, bounded recent
  textual turn memories (six turns by default), and a rolling memory capped at
  2,000 characters. Audio is transcribed once. Qwen receives the transcript for
  its reply and private analysis; Deepgram synthesizes only the reply. Previous
  raw uploads never enter later turns.
- All prompts are in `app/prompts.py`. Spoken content, turn analysis and final
  summaries are separate calls. Internal JSON is never synthesized as speech.
- Natural endings and explicit `/end` generate a validated high-level summary;
  summary inference has a maximum 30-second deadline. Model/format failure uses
  a generic inconclusive result. Analysis failure returns available speech while
  conservatively keeping the session active. A task cannot complete merely from
  an opening prompt; resident evidence is required.
- A five-second maintenance loop expires inactive sessions after 900 seconds by
  default. Timeout uses a conservative inconclusive event without waiting for
  the model, so a GPU outage cannot prevent private-memory cleanup. Memory and
  the original goal are erased on finalization and shutdown. Multipart uploads
  may use framework-managed temporary spooling during the current HTTP request;
  they are closed after reading and never intentionally archived.
- High-level summaries are model-generated, length/schema validated, and reject
  quoted/multiline dialogue. Semantic minimization still needs evaluation with
  the deployed model; schema checks alone cannot establish that quality.
- Session tombstones retain only metadata and a final high-level event for 24
  hours, supporting repeated `/end` and `/requests`. Retention is configurable;
  unknown IDs return 404 and retained ended/expired IDs return 410 on `/generate`.
  Request deduplication is within this process and retention window; reusing an
  ID with a different body returns 409. `/generate` has no turn retry key: callers
  should not automatically replay an ambiguous failed HTTP response.
- Active sessions plus tombstones are capped at `MAX_SESSIONS` (256). At capacity
  new sessions return 503 until old tombstones expire. Session operations serialize
  per ID; unrelated sessions remain independent.

Uploads allow WAV/MP3/FLAC/OGG/MP4 audio, with 10 MiB per file by default, bounded
text and a total request-body limit. Deepgram decodes the media. Images and empty
transcriptions return 422, empty input 400, unsupported types 415, oversized uploads
413, and provider failures 502. A missing Deepgram key returns 503. Failed synthesis
does not commit the turn or finalize the task; retrying an existing session keeps
its prior context.
Errors and routine application logs do not echo model bodies or private inputs.

## Dedicated-server delivery

`DEDICATED_SERVER_URL` is the **complete final-event endpoint**, not a base URL.
Only this schema crosses that boundary:

```json
{
  "session_id": "a-generated-session-id",
  "request_id": "req_456",
  "type": "check_in",
  "status": "completed",
  "summary": "Grandma reported having breakfast."
}
```

General conversations have `request_id: null`, `type: "conversation"`. Final
statuses are completed, cancelled, failed, or inconclusive. The SQLite outbox at
`OUTBOX_PATH` stores only high-level events, never temporary memory or media.
It survives restarts and retries failures with bounded exponential backoff and
a ten-second HTTP timeout. Delivery runs separately from conversation handling.
An empty destination retains events until configured. Successful delivery clears
the local summary payload, retaining an ID receipt to prevent enqueue duplicates.
Pending results and ID receipts are not automatically discarded; monitor disk
usage and back up the outbox if durable delivery matters.

The receiver must deduplicate `Idempotency-Key: <session_id>` (also present in the
body). HTTP delivery is **at least once**: a lost acknowledgment can cause retry.
Duplicate local finalization enqueues only one event. A disk enqueue failure keeps
the minimal event in RAM for retry; explicit `/end` returns 503, while natural
completion still returns available audio. A process crash during a disk failure
can lose that not-yet-durable result. A remote outage alone does not lose results.

## Structure and verification

```text
app/config.py                  environment only
app/prompts.py                 all model prompts
app/schemas.py                 public and internal typed schemas
app/api/                      validation, authentication, HTTP responses
app/services/agent.py          conversation orchestration
app/services/qwen.py           text-only vLLM adapter
app/services/deepgram.py       transcription and WAV synthesis
app/services/dedicated_server.py summary-only durable delivery
app/sessions/                 replaceable store and session lifecycle
```

The agent returns typed audio results independent of HTTP, allowing a future
binary/WebSocket transport without moving history to the phone. Actual streaming
and distributed sessions are not implemented.

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r robot_backend/requirements.txt
.venv/bin/python -m unittest discover -s robot_backend/tests -v
.venv/bin/python -m compileall -q robot_backend/app
```

The speech regression tests use standard-library unittest and mocked HTTP.
Use the API examples above with synthetic media for deployment checks. Syntax and
mocked HTTP checks do not establish model quality, actual audio playback, real
phone delivery or physical robot behavior.
