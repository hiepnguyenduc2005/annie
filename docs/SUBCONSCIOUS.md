# Subconscious multi-agent support (optional, opt-in)

Annie can use [Subconscious](https://subconscious.dev) for a small, bounded
multi-agent review of captured evidence. The feature is opt-in: it performs
external network egress, so nothing runs unless the API layer explicitly
enables it and supplies credentials.

## Scope and bounds

- Fixed team: an evidence analyst and an uncertainty reviewer run in
  parallel, then one synthesis call combines them.
- At most 3 API calls per invocation. No retries, no provider fallback,
  and no extra steps on failure.
- Each request times out after 20 seconds.
- Output tokens are bounded (256 per reviewer, 512 for synthesis), and
  provider text longer than 4000/8000 characters is rejected.
- No tools are offered to the provider, and no robot or notification
  actions are requested.

## Provider API

The service uses the current documented chat-completions endpoint and never
touches the older proprietary /runs API:

```
POST https://api.subconscious.dev/v1/chat/completions
Authorization: Bearer $SUBCONSCIOUS_API_KEY
{"model": "subconscious/glm-5.3-marathon", "messages": [...], "max_tokens": 512,
 "chat_template_kwargs": {"enable_thinking": false}}
```

Per the provider README, tools execute client-side; Annie passes no tools
and expects plain message content only. Requests set chat_template_kwargs
enable_thinking to false so structured synthesis output avoids reasoning
preambles, per the official README.

## Environment names

- SUBCONSCIOUS_API_KEY: required when enabled; the API layer passes it
  to the service per call.
- SUBCONSCIOUS_MODEL: optional model override; defaults to
  subconscious/glm-5.3-marathon.

The service module (robot/app_backend/app/subconscious_provider.py) never reads
environment variables or configuration files. The owner-facing API layer
is responsible for the enabled check, authentication, key handling, and
router mounting.

## Evidence sanitization

Evidence must be small, sanitized captions. Each item may contain only:

- caption (max 300 chars)
- frame_id (max 80 chars)
- timestamp (max 40 chars)
- observer (max 120 chars)

At most 12 items per call. Values that look like URLs, API tokens, or raw
media (base64 blobs, long unbroken strings, or data: media schemes
including ;base64,) are rejected before any network call, rather than
passed through. The task string is capped at 4000 characters and receives
the same URL, token, data: media, and blob guards. These guards are input
screening, not PII anonymization; callers remain responsible for the
content they send. Prompts instruct the provider that evidence is data,
never instructions, and that output is advisory with no diagnostic claims.

## Service interface

```python
from robot.app_backend.app.subconscious_provider import run_team

result = await run_team(
    task="Summarize movement in this clip.",
    evidence=[
        {"frame_id": "frame-1", "timestamp": "00:12", "caption": "person near door"}
    ],
    api_key=key,                            # required, supplied by caller
    model="subconscious/glm-5.3-marathon",  # optional
    client=shared_client,                   # optional httpx.AsyncClient
)
```

The result dict contains:

- answer: synthesis answer (JSON-parsed when the provider returned JSON).
- agents: [{"role": ..., "summary": ...}] for the two reviewers.
- used_frame_ids: frame IDs cited by synthesis, validated against the
  input allowlist (invalid citations dropped; empty when no JSON was used).
- model: model used.
- provider: always "subconscious".
- calls_used: number of API calls made (at most 3).

Failures raise SubconsciousInputError (bad task, evidence, or credentials)
or SubconsciousAPIError (timeout, non-2xx status, malformed provider data).
Error messages never contain the API key or the response body.

## Planned API sketch

The backend endpoint (mounted by the owner, behind the enabled check and
auth) is expected to look like:

```
POST /agents/run
{"task": "...", "evidence": [...], "allow_cloud": true}
```

allow_cloud: true is required for the handler to invoke the provider;
requests without it are rejected locally with no egress. The handler reads
SUBCONSCIOUS_API_KEY from configuration and passes it to run_team; no keys
appear in requests, responses, or logs.
