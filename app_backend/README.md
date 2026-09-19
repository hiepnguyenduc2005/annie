# Annie local demo backend

The approved robot-to-app boundary now includes typed maps, captions, observer
poses, event evidence, released crops, transcripts, and two-way commands. See
[contract v0.1](../contract/README.md). The older status-only envelope is a
compatibility option; full frames remain on the trusted local compute network.

Python 3.10+, FastAPI, and SQLite. Run one worker: the event bus and check-in
state are in process. This is a local demonstration, not a medical device or a
working robot integration. No calls reach hardware, cloud vision, or speech.

## Setup and run

From the repository root:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r app_backend/requirements.lock
.venv/bin/uvicorn app_backend.app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open http://127.0.0.1:8000/app/ for the dashboard when the root `frontend/` directory
is present. `/` preserves the welcome response; `/health` returns `{"status":"ok"}`.
The server starts empty. `POST /demo/seed` explicitly loads synthetic data.

Environment variables:

- `ANNIE_MODE`: `demo` (default) or `live`. Live disables all demo controls but
  does not connect any real providers or hardware automatically.
- `ANNIE_DB_PATH`: SQLite location; default `.data/annie.sqlite3`, relative to
  the process working directory. Events and the latest 1,000 perception memories
  persist. Current robot/map/check-in state and queued commands do not survive
  restart; this demo is unsuitable for unattended monitoring.
- `ANNIE_API_TOKEN`: optional bearer token. Without it, data routes accept only
  loopback clients. With it, every data request requires `Authorization: Bearer …`.
  The application ignores forwarded client headers; keep `--no-proxy-headers`.

To load the repository `.env` explicitly, append `--env-file .env` while
running from the repository root. Do not publish secrets or bind this demonstration to a
public interface. Browser origins must match the request host and scheme.
WebSocket tokens travel only in the first message, never the URL. Root, health,
and static dashboard assets are public; resident data routes are protected.
`/openapi.json` uses the same authentication as data routes. Swagger UI is
disabled. Request bodies are limited to 600,000 bytes. Allowed hosts default to
localhost/loopback; set `ANNIE_ALLOWED_HOSTS` to include an intended LAN address
when testing on a phone, and configure `ANNIE_API_TOKEN` first.

`POST /agents/run` supports the optional advisory Subconscious team. It requires
`SUBCONSCIOUS_API_KEY`, `ANNIE_ENABLE_CLOUD_AGENTS=true`, and request
`allow_cloud:true`. It sends only the supplied task/evidence and does not pull
resident data automatically. See [setup and evidence format](../docs/SUBCONSCIOUS.md).

## API

| Route | Behavior |
| --- | --- |
| `GET /status` | Latest dog/perception, mode, pending check-in, integration flags |
| `GET /map` | Latest map; 404 until received or seeded |
| `GET /events?since=0` | Events with `ts > since`, ascending timestamp then ID |
| `POST /events/{event_id}/ack` | `{"by":"family"}`; idempotent receipt acknowledgment |
| `POST /say` | `{"text":"Hello"}`; returns queued command |
| `GET /commands` | Latest 100 queued commands, oldest first |
| `POST /commands` | `{"cmd":"stop"}` or resume/look/goto; goto needs a known waypoint |
| `POST /query` | `{"text":"glasses"}`; local lexical caption matches and evidence citations |
| `GET /frames/{frame_id}` | Crop-only bytes; 404 if missing (demo stores no crops) |
| `POST /ingest` | `{"channel":"brain.perception","data":{…}}`; strict channel model |
| `POST /demo/seed` | Synthetic home map, idle robot, safe bed observation |
| `POST /demo/scenario` | `{"scenario":"fall"}`; safe_bed/fall/help/okay/timeout |
| `WS /live` | Initial `{type:"snapshot",data:status}`, then channel envelopes |
| `POST /api/messages` | `{"author_id":"zach","text":"…"}` → 202 `{run_id,status:"dispatched"}`; never blocks on robot_backend |
| `GET /api/runs/{run_id}` | Run status plus its ordered event list; 404 if unknown |
| `GET /api/thread` | The family message thread, oldest first |
| `WS /ws/family` | Initial `{type:"snapshot",data:{thread,runs}}`, then `message`/`run_status`/`run_event` envelopes |
| `POST /internal/events` | Called by robot_backend only; `X-Internal-Secret` header required, not the family token |

When a token is configured, send `{"token":"…"}` as the first WebSocket message
within five seconds; otherwise the initial snapshot arrives immediately. Clients
must reconnect and fetch events after a dropped connection. Each subscriber has
100 buffered updates; overflow replaces the backlog with a `resync` envelope.
On resync, refetch status/map/events/commands. With the millisecond `since`
cursor, overlap the last millisecond and deduplicate IDs, or refetch all events,
so an equal-timestamp arrival is not missed.

Input channels: `dog.status`, `dog.map`, `brain.perception`, `voice.heard`.
Output channels also include `event`, `command`, and `checkin` (pending data or
null). Exact schemas are generated in `contract/schemas.json`. Unknown fields,
including raw image fields, are rejected. `dog.frame` is never accepted.
Coordinates are metres and describe the observing robot, not the resident or
object. Timestamps are integer Unix milliseconds. Image evidence is null unless
an actual trusted crop exists; the demo never fabricates photographs.

**Stop is only a queued software request. It is not a hardware emergency stop.**
No command response means execution succeeded. Acknowledging an event records
family receipt; it does not resolve physical safety or cancel its check-in.

## Family message relay

`POST /api/messages` accepts a free-text message from one of a hardcoded
three-person household (`jeanine`, `zach`, `ellis`; no signup, no JWT) and
returns instantly with a run ID; the actual navigate/speak/listen/recall/speak
sequence is dispatched to `robot_backend` in a background task. This state is
entirely in-memory (no SQLite) and does not survive a restart — a deliberate,
separate carve-out from the rest of this service (see DEC-006). The interface
this dispatch call and `POST /internal/events` implement is documented in
[contract/family_messages.md](../contract/family_messages.md).

Environment variables:

- `ROBOT_BACKEND_URL`: the GX10's LAN address, e.g. `http://192.168.1.42:8001`.
  Never `localhost` — the two services run on different machines. Change this
  one value (not code) when the network changes.
- `ANNIE_ROBOT_DISPATCH_TIMEOUT_S`: per-attempt timeout, default 3s. Dispatch
  makes exactly one attempt plus one retry; if both fail, the run is marked
  `unreachable` and `POST /api/messages` has already returned regardless.
- `ANNIE_INTERNAL_SECRET`: shared secret `robot_backend` must send as
  `X-Internal-Secret` on `POST /internal/events`. Required — an unconfigured
  secret rejects every request, it never falls open.
- `ANNIE_FAMILY_MOCK_ROBOT`: `true` replaces the real dispatch with a canned
  in-process event sequence (with delays), so the iOS app and family thread
  can be demoed with no GX10 and no dog.

For the phone/robot LAN demo, run `uvicorn` with `--host 0.0.0.0` (not
`127.0.0.1`) and add the Mac's LAN address to `ANNIE_ALLOWED_HOSTS` — both the
iPhone and `robot_backend`'s calls to `/internal/events` need it there, or
`TrustedHostMiddleware` rejects them before they reach the handler.

Two scripts help verify this without waiting on the actual robot:

```sh
# Prints PASS/FAIL for reaching ROBOT_BACKEND_URL in about five seconds.
.venv/bin/python app_backend/scripts/check_robot_backend.py

# Posts a message and prints each run event as it arrives (works great with
# ANNIE_FAMILY_MOCK_ROBOT=true and no GX10 at all).
.venv/bin/python app_backend/scripts/demo_family_message.py --text "How are you feeling today?"
```

## Rules and memory

Two distinct, consecutive observations within five seconds, each with a person
lying on floor/chair and confidence at least 0.8, emit `fall_suspected` and queue
an eight-second spoken check-in. Bed observations are safe. Unknown and low
confidence reset the candidate; stale, future, duplicate, and out-of-order frames
are ignored. No repeat alert occurs during an episode until a valid safe frame
arrives after the check-in closes. Errors from vision adapters must be omitted,
not converted into confident observations.

Only an explicit okay/help reply with confidence at least 0.8, matching
`event_id`, timestamped inside the pending window, affects the check-in. Ambiguous
speech does not cancel it. Help escalates immediately. A 250 ms ticker escalates
an unanswered check-in after eight seconds, emitting `checkin_no_reply` and
`fall_confirmed`. The latter means **escalation, not a medically proven fall**.
The demo timeout control evaluates the check-in deadline immediately but stamps
resulting events with the current clock, keeping incremental retrieval valid.
Distinct frames may share a millisecond; UUIDs distinguish them. Map changes
reset a pending candidate without discarding evidence for an existing check-in.

Caption retrieval searches at most 1,000 saved observations and returns up to
three quoted caption values and their frame IDs/timestamps/observer poses. It
performs no diagnosis, semantic reasoning, or external model call. No lexical
match returns `I wasn’t there for that`. SQLite stays local; retention beyond the
bounded memory table, encryption, and production access control are future work.
Events currently persist without automatic expiration.

Redis ingress, camera/frame processing, crop generation, SLAM, physical motion,
speech execution, external vision, and semantic retrieval are not implemented in
this core. Optional provider modules are not automatically invoked.

## Verify

From the repository root:

```sh
PYTHONPATH=app_backend .venv/bin/python -m pytest app_backend/tests -q
.venv/bin/python contract/export_schemas.py --check
```

Regenerate schemas after changing models:

```sh
.venv/bin/python contract/export_schemas.py
```

Tests use in-memory/temporary SQLite, injected clocks, and TestClient. They do
not wait eight seconds or contact providers/hardware. `requirements.lock` pins
the tested development dependency set; review updates before deployment.
