# Family message dispatch — app_backend to robot_backend (draft v0.1)

Added 2026-09-19 alongside the async family-messaging feature in `app_backend`.
This is a **new** boundary; nothing in `robot_backend` implements its side yet
— the demo runs against `app_backend`'s mock mode instead (see below). The
`robot_backend` owner should implement the two calls below to connect for real.

## Deployment

`app_backend` runs on the family-facing machine and listens on `0.0.0.0` so
the iPhone app can reach it over the LAN. `robot_backend` runs on the GX10 in
the resident's home. `app_backend` reaches `robot_backend` at `ROBOT_BACKEND_URL`
(a plain LAN URL, e.g. `http://192.168.1.42:8001`) — never `localhost` — and it
WILL change between networks, so it is one env var on the app_backend host,
never a code edit. `robot_backend` calls back into `app_backend` at whatever
LAN address the app_backend host advertises; both directions need that address
listed in `ANNIE_ALLOWED_HOSTS` on the app_backend host (see `.env.example`
and `app_backend/README.md`).

## `app_backend` → `robot_backend`: `POST {ROBOT_BACKEND_URL}/dispatch`

Fire-and-forget from app_backend's perspective: it sends this from a
background task immediately after accepting `POST /api/messages`, with an
explicit timeout (`ANNIE_ROBOT_DISPATCH_TIMEOUT_S`, default 3s) and exactly
one retry. If both attempts fail (timeout, connection error, or a non-2xx
response), the run is marked `unreachable` and a synthetic `unreachable` event
is recorded. `POST /api/messages` itself has already returned 202 before any
of this runs, and never blocks on it.

Request:

```json
{"run_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81", "author_id": "zach",
 "author_name": "Zach", "text": "How are you feeling today?", "dispatched_at": 1789800000123}
```

Any 2xx response is treated as "robot_backend accepted the run"; the run moves
to `running`. The response body is currently ignored. `robot_backend` should
run its navigate/speak/listen/recall/speak sequence asynchronously after
returning, and report progress through the endpoint below — app_backend does
not poll or hold a connection open waiting for it.

## `robot_backend` → `app_backend`: `POST /internal/events`

Called by `robot_backend` (or a stand-in) as the physical sequence progresses.
Requires a shared secret, not the family `ANNIE_API_TOKEN`: header
`X-Internal-Secret: <ANNIE_INTERNAL_SECRET>`, exact match, checked in constant
time. Missing or wrong secret is 401; an unconfigured secret on the app_backend
side rejects every request — fail closed, never open. The robot_backend host's
LAN address also needs to be in `ANNIE_ALLOWED_HOSTS`, or the request never
reaches the handler at all.

Request:

```json
{"run_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81", "kind": "speaking",
 "payload": {"text": "Hi Jeanine, Zach says..."}, "at": 1789800002500}
```

`kind` is one of `navigating`, `arrived`, `speaking`, `listening`, `heard`,
`recalling`, `recalled`, `completed`, `failed`. app_backend adds two derived
fields to the stored event before returning it to clients — `summary` (a
display line) and `speaker` (`annie`, `resident` or `system`). Those are
app_backend's own output: **do not send them**, and expect clients to render
them instead of reaching into `payload`, so an unfamiliar payload degrades to
a readable line rather than breaking a UI. `payload` is an open,
step-specific dict (free-form; bounded only by the app's overall request-size
limit). `completed`/`failed` are terminal: they close the run, and any further
event for that `run_id` is rejected with 409. An unknown `run_id` is 404.
Successful ingestion returns 202 with the stored event.

## Run lifecycle

`dispatched` (accepted by app_backend, dispatch in flight) → `running`
(robot_backend acknowledged the dispatch, or any progress event arrived) →
`completed` | `failed` (from an explicit terminal event) | `unreachable`
(dispatch never got through to robot_backend). `unreachable`, `completed`, and
`failed` are all terminal — no further event is accepted once a run reaches
one of them.

## Testing without the GX10

`ANNIE_FAMILY_MOCK_ROBOT=true` on the app_backend host replaces the HTTP call
above with a canned in-process event sequence (with delays), driving the same
run/event/WebSocket path so the iOS app and family thread can be demoed with
no GX10 and no dog. See `app_backend/README.md`.
