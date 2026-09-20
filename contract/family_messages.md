# Family message dispatch — app_backend to robot_backend (draft v0.1)

Added 2026-09-19 alongside the async family-messaging feature in `app_backend`.
Both sides are implemented: `robot/dog/missions/errand.py` serves `/dispatch`
and reports progress through `/internal/events`, and the first app-to-dog
missions ran on the physical Go2 on 2026-09-20. This document remains the
authority on the wire format. `app_backend/scripts/fake_robot.py` is a
standard-library stand-in for working on the app without the dog.

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

## Implementing the robot side (handoff)

Everything this needs already exists under `robot/`; what is missing is the
adapter that joins it to the run model above. Suggested mapping:

| Step | Existing piece | Event to post |
| --- | --- | --- |
| Decide and phrase | `robot/robot_backend/app/brain/planner.py` `POST /plan` — takes a free-text `goal` (use the message text), `waypoints`, `memories`; returns one action (`goto`/`say`/`look`/`wait`/`stop`) | — |
| Walk to her | planner `goto` + `robot/simulation/bridge.py`'s existing command loop | `navigating`, then `arrived` on the execution receipt |
| Speak | planner `say` + `robot/simulation/native_audio.py` | `speaking` (put the spoken line in `payload.text`) |
| Hear her | `robot/simulation/local_stt.py` | `listening`, then `heard` (`payload.transcript`) |
| Remember | `robot/app_backend/app/episodic_memory.py` `ask()` | `recalling`, then `recalled` (`payload.note`) |
| Finish | — | `completed`, or `failed` with `payload.error` |

Six things that will bite, all verified against the current code:

1. **Terminal states are final.** After `completed` or `failed`, any further
   event for that `run_id` is rejected with 409. Post `completed` last.
2. **Don't send `summary` or `speaker`.** app_backend derives them; extra keys
   are rejected by the strict model.
3. **The planner prompt caps spoken lines at 80 characters** (the schema allows
   500). The demo's line is 92, so either relax the prompt or shorten the line,
   or the model will truncate the thought.
4. **Episodic memory refuses when it has no evidence**, by design. The fact the
   dog is meant to recall has to be in *its* store — app_backend's seeded copy
   is a separate store and is not visible to the robot.
5. **Point at the right backend.** These endpoints are on the top-level
   `app_backend` (the Mac), not `robot/app_backend`. The Mac's LAN address must
   also be in its `ANNIE_ALLOWED_HOSTS`, or `TrustedHostMiddleware` rejects the
   request before the handler ever runs.
6. **`/dispatch` should ack immediately** and run the errand afterwards.
   app_backend gives it ~3s with one retry, then gives up and marks the run
   `unreachable`.

You can develop against a running app_backend without touching the app: post
events by hand and watch them appear in the phone and web clients.

```sh
curl -X POST http://<mac-lan-ip>:8000/internal/events \
  -H "X-Internal-Secret: $ANNIE_INTERNAL_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<id from POST /api/messages>","kind":"speaking",
       "payload":{"text":"Jeanine, Zach asked me to find you."},"at":1789800002500}'
```

## Household records: three more calls each way

Added 2026-09-19 alongside the persistent household schema (profiles,
messages, reminders, reminder history, emergencies). These are separate from
the run/event flow above: runs carry live progress, these carry the record.

app_backend → robot_backend, same timeout and single retry as `/dispatch`,
but failures are recorded rather than raised, because the record is already
stored:

| Call | Body |
| --- | --- |
| `POST {ROBOT_BACKEND_URL}/messages` | `{message_id, dog_user_id, app_user_id, text}` |
| `POST {ROBOT_BACKEND_URL}/reminders` | `{reminder_id, dog_user_id, hour, item}` |

robot_backend → app_backend, all requiring `X-Internal-Secret`:

| Call | Body | Effect |
| --- | --- | --- |
| `POST /internal/message-reply` | `{message_id, text, source?}` | Appends the dog's action summary, or the resident's spoken response, to that message's `texts`. `source` is `robot` (default) or `resident`. 404 if the message is unknown. |
| `POST /internal/reminder-history` | `{reminder_id, description, timedate?}` | Records what actually happened, e.g. "grandma took her pills". 404 if the reminder is unknown. |
| `POST /internal/emergencies` | `{dog_user_id, description, timestamp?}` | Records an emergency and pushes it to connected family clients immediately. 422 if the dog user is unknown. |

`timedate` and `timestamp` are integer Unix milliseconds and default to
arrival time. Omit them only when the robot has no better clock than ours.

## Testing without the GX10

`ANNIE_FAMILY_MOCK_ROBOT=true` on the app_backend host replaces the HTTP call
above with a canned in-process event sequence (with delays), driving the same
run/event/WebSocket path so the iOS app and family thread can be demoed with
no GX10 and no dog. See `app_backend/README.md`.
