# GX10 brain stand-in API

This independent service runs image inference against an explicitly configured
OpenAI-compatible provider. It simulates the GX10 API boundary, not GX10 hardware
performance. It does not generate observations from scenario labels and has no
incident, voice, or robot-control authority.

Run from the repository root (FastAPI, httpx, Pillow, and uvicorn required):

```sh
.venv/bin/uvicorn robot.robot_backend.app.brain.api:app --host 127.0.0.1 --port 8002 --no-proxy-headers --env-file .env
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `ANNIE_VISION_MODE` | `disabled` (default), `local`, or explicit `cloud`. |
| `ANNIE_VISION_BASE_URL` | Required provider API prefix, e.g. `http://127.0.0.1:9000/v1` locally or `https://api.openai.com/v1` for cloud. The service appends `/chat/completions`. |
| `ANNIE_VISION_MODEL` | Required image-capable model identifier; no automatic model choice. |
| `ANNIE_VISION_API_KEY` | Provider credential; cloud requires it. `OPENAI_API_KEY` fallback is accepted only for exact `api.openai.com` host. |
| `ANNIE_VISION_TIMEOUT_S` | Total provider deadline, default 30 seconds, range 1–120. |
| `ANNIE_VISION_MAX_CLOUD_CALLS` | OpenRouter persistent attempt limit, default 100, maximum 1000. |
| `ANNIE_VISION_BUDGET_USD` | OpenRouter reservation budget, default 20, positive and at most 20 USD. |
| `ANNIE_VISION_USAGE_PATH` | OpenRouter atomic reservation ledger, default `.data/vision-usage.json`. Preserve across restarts. |
| `ANNIE_API_TOKEN` | Inbound bearer authentication; without it only loopback clients are accepted. |
| `ANNIE_ALLOWED_HOSTS` | Explicit inbound host allowlist; default loopback names. |

Local mode requires HTTP to a loopback literal IP or `localhost` (pinned to
127.0.0.1). A remote GX10 requires a local tunnel. Cloud requires HTTPS and accepts
only requests labeled `simulation_render`. Source is a trusted producer's
assertion, not cryptographic attestation: do not connect resident/hardware
producers to the cloud service. Hardware source is rejected before inference.
Cloud permission here covers synthetic rendered simulation images only.

Provider HTTP ignores proxy environment variables, refuses redirects, bounds
responses at 65,536 bytes, and makes one attempt without retry or fallback.
One inference runs at a time; concurrent requests receive 429. Invalid active
configuration fails startup. Default disabled configuration makes no calls.

## Request and response

`GET /health` and `GET /health/config` report configuration/readiness, model,
deadline, and image limits. They do not contact the provider or reveal endpoint,
credential, raw response, or frame data. Both require normal authentication.

`POST /infer` accepts exactly:

```json
{
  "frame_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81",
  "ts": 1789800000123,
  "pose": {"x": 1.2, "y": 2.3, "yaw": 0.4, "map_id": "sim-home-1"},
  "source": "simulation_render",
  "jpeg_b64": "<base64 JPEG bytes, no data-URL prefix>"
}
```

`frame_id` is a canonical lowercase UUID string. `ts` is a nonnegative integer
Unix capture timestamp in milliseconds, never inference completion time.
All pose fields are required: finite numeric metres/radians and a 1–100 character
map ID. The pose locates the observing robot. Numeric strings and extra keys
(including scenario/ground-truth labels) are rejected. Source is exactly
`simulation_render` or `hardware`; cloud allows only the former.

JPEGs are decoded, checked, and re-encoded to remove embedded metadata before
provider egress. Maximum encoded JPEG size is 1,000,000 bytes; maximum width
and height are each 1280 pixels (640×480 recommended). Base64 is bounded at
1,333,336 characters; total JSON request limit is 1,337,432 bytes, enforced for
streamed bodies as well. Invalid images or oversized dimensions fail before
provider invocation. Frames are never saved by this service.

Successful response:

```json
{
  "perception": {
    "schema_version": "0.1",
    "ts": 1789800000123,
    "frame_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81",
    "person": true,
    "posture": "lying",
    "location": "floor",
    "confidence": 0.91,
    "caption": "A person appears to be lying on the floor.",
    "pose": {"x": 1.2, "y": 2.3, "yaw": 0.4, "map_id": "sim-home-1"}
  },
  "provider": {"mode": "cloud", "model": "configured-model"},
  "source": "simulation_render",
  "latency_ms": 825.3
}
```

The nested `perception` validates against `robot.app_backend.app.models.Perception`;
only that object enters the app's `brain.perception` channel. Capture UUID,
timestamp, and pose come from the input and are preserved. Provider receives
only sanitized pixels plus a fixed observation instruction, never the timestamp,
UUID, pose, source label, or simulation ground truth. The provider must return
strict JSON with all six observation keys, actual boolean/numeric types, posture
`standing|sitting|lying|unknown`, location `bed|floor|chair|unknown`, confidence
in `[0,1]`, and caption of at most 2000 characters. Unknown is uncertainty,
and confidence is a model estimate, not a calibrated clinical measurement.

When reported by the provider, `provider.usage` includes the allowlisted
`prompt_tokens`, `completion_tokens`, `total_tokens`, and `cost_usd` (from
OpenRouter's `usage.cost`). Missing usage/cost stays absent, never zero. This is
reported accounting, not a spending limit. A cloud runner must separately enforce
its authorized budget for providers other than the approved OpenRouter route.

The approved OpenRouter vision models are listed below; `xiaomi/mimo-v2.5:floor`
also supports the approved audio route. All routes enforce 512 output tokens
via `max_tokens`, no provider fallback, and text-only output with no tools.
Reasoning is disabled except for Gemini 3.8, whose smallest supported level is
`low`; its reasoning content is excluded from returned text. References:
[OpenRouter reasoning](https://openrouter.ai/docs/use-cases/reasoning-tokens),
[Google Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).
The Gemini 3.8 route excludes Flex endpoints to avoid their latency tradeoff.
Per-model provider price
ceilings in USD per million tokens and the conservative reservation held per
attempt (full verified context at the ceiling plus 512 output tokens, kept on
failures and timeouts):

| Model | Context tokens | Prompt cap | Completion cap | Reservation |
| --- | --- | --- | --- | --- |
| `google/gemini-3.8-flash:floor` | 1,048,576 | $0.76 | $3.76 | $0.80 |
| `google/gemini-2.5-flash-lite:floor` | 1,048,576 | $0.11 | $0.41 | $0.12 |
| `qwen/qwen3-vl-32b-instruct:floor` | 131,072 | $0.11 | $0.42 | $0.02 |
| `deepseek/deepseek-v4.1-flash:floor` | 1,048,576 | $0.31 | $1.21 | $0.50 |
| `xiaomi/mimo-v2.5:floor` | 1,050,000 | $0.15 | $0.29 | $0.20 |

Reservations draw on one shared persistent ledger across all approved models:
the budget caps the combined total, not each model separately, so mixing models
cannot exceed `ANNIE_VISION_BUDGET_USD` (default $20 total, absolute ceiling
$20). Default 100 attempts of the cheapest model reserves at most $2. A
corrupt/unwritable ledger fails closed. Validated successful responses can settle
their own reservation once to reported provider cost. Historical reservations
and uncertain failures retain the full debit; missing cost is not zero.
Per-model charges and outstanding reservations must agree with the total. Do not delete or change
the ledger path to reset an active budget. This meter covers only requests made
through this service and depends on the provider honoring its documented
limits. Price-cap semantics: [OpenRouter official cost
guide](https://openrouter.ai/blog/tutorials/how-to-get-the-lowest-cost-llm-inference-on-openrouter/).
Model facts are owner-verified against the public OpenRouter catalog:
[google/gemini-3.8-flash](https://openrouter.ai/google/gemini-3.8-flash),
[deepseek/deepseek-v4.1-flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash)
and
[xiaomi/mimo-v2.5](https://openrouter.ai/xiaomi/mimo-v2.5).

The service uses image `image_url` content with a base64 data URI and
`response_format: {"type":"json_object"}` for provider compatibility; strict
application validation remains mandatory. OpenRouter requests use the widely
supported `max_tokens` and the model-specific reasoning settings above; other providers
receive `max_completion_tokens`. Wire format reference:
[official OpenAI Chat Completions documentation](https://developers.openai.com/api/reference/resources/chat).

Errors contain no observation: 401 authentication, 403 origin/egress/client
boundary, 413 request bytes, 422 input/image validation, 429 busy, 502 provider
HTTP/malformed/incomplete/oversized output, 503 disabled, 504 timeout. Provider
errors never become synthetic fallback observations. A retry reuses the capture
metadata; the service does not cache responses or retry paid calls. App ingestion
deduplicates frame IDs; producers should avoid sending the same frame twice.

Verification uses synthetic JPEGs and mocked HTTP, not GX10 or paid inference:

```sh
.venv/bin/python -m pytest robot/robot_backend/tests/test_brain.py -q
```

## Goal-directed planning

`POST /plan` accepts `observation` (the rendered frame contract), `goal`, known
`waypoints`, bounded `recent_outcomes`, cited `memories`, and `progress`.
Progress contains measured completed visits, unvisited waypoint IDs, incident
receipts, and an optional `last_person_sighting` preserving its original frame,
time, and camera pose. Cross-map and future sightings are excluded from context.
The bounded context contains at most six rolling memories; the retained positive
sighting is separate. Captions and execution details are data, not instructions.

The response echoes frame identity and contains perception plus one action:
`goto` (waypoint ID), `say` (text), `look`, `wait`, `stop`, or `finish`.
Each action includes a brief user-visible reason. The simulator validates current
map, five-second capture age, goal revision, command queues, and execution gates
before dispatch. A selected action is not an executed command.

`finish` is the model's declaration that a finite goal is complete. It sends no
motor command. The bridge requires accepted evidence, no pending incident, and
terminal motion/audio receipts before recording `goal_completion`. Further
planning for that exact map and goal revision stops; telemetry continues. A new
operator goal/resume permits another run. Completion wording remains a model
claim and must be assessed against its cited observations and execution receipts.
