# Annie integration contract v0.1

Working contract for the staged demo, based on the team's
[original proposal](../docs/hackmit-2026/sources/proposed-contract.txt).
[schemas.json](schemas.json) is generated from backend models;
`/openapi.json` describes the implemented REST API. This contract distinguishes
the runnable local mock from target hardware and service integrations.

The user expanded the earlier status-only boundary on 2026-09-19. Typed maps,
observer poses, captions, event evidence, approved crops, relevant transcripts,
and two-way commands may cross the robot-to-app boundary. The legacy
`shared/messages.py` status envelope is an optional compatibility format, not
the limit of this contract. Full frame transport remains local as specified
below; authenticated access and bounded payload validation still apply.

## Conventions

- JSON; schema version `0.1`; integer Unix milliseconds; UUID frame/event IDs.
- Pose `{x,y,yaw,map_id}` uses metres and radians in a named DimOS map frame.
  It is the robot/camera observation position, not a person's measured location.
- Preserve frame IDs and capture timestamps. Pair frame and pose timestamps in
  the body adapter. Change map ID on reset; do not compare positions across maps
  without a calibrated transform. Hardware synchronization remains unverified.
- Confidence is in `[0,1]`; unknown is explicit. Validate at every boundary,
  bound sizes, and reject incompatible payloads. Repeated deliveries retain IDs.
- Queue acceptance, consumer acknowledgement, and physical execution are distinct.

## Transport and channels

Target: body on the DimOS laptop, brain on GX10, Redis on the trusted local
network, and FastAPI REST/WebSocket to the family app. The current milestone
uses an in-process bus, SQLite persistence, and `POST /ingest`. A Redis adapter
is still required. Pub/Sub alone does not supply durable replay or delivery.

| Channel | Direction | Payload/purpose |
| --- | --- | --- |
| `dog.frame` | Body → brain only | Proposed 2 Hz local JPEG, timestamp, UUID, dimensions, synchronized observer pose; never the app/cloud. |
| `dog.status` | Body → app | State, battery percentage, waypoint, pose, timestamp. |
| `dog.map` | Body → app | Map ID, occupancy PNG, origin, resolution, waypoints; target every 30 s/on change. |
| `dog.cmd` | Brain/app → body | Identified goto/stop/resume/look command. Software stop is not a hardware emergency stop. |
| `brain.perception` | Brain → policy/app/memory | Person presence, posture, location category, confidence, caption, frame ID, timestamp, observer pose. |
| `brain.event` | Policy → app/voice | Check-in/incident event, severity, evidence IDs, optional released crop URL. |
| `voice.say` | Policy/app → voice | Bounded text, priority, optional cache key, command ID. |
| `voice.heard` | Voice → policy/app | Transcript, confidence, timestamp, resident speaker, correlated incident ID. |
| `memory.write` | Brain → memory | Caption, entities/room if available, frame ID, timestamp, observer pose. |

Only status, map, perception, and heard channels are currently accepted by
`/ingest`. Other channels describe planned adapters. Raw frame bytes are not
accepted by app ingress, memory, event, or advisory agent schemas.

## REST and WebSocket

Use `Authorization: Bearer <ANNIE_API_TOKEN>` when configured. Without a token,
the demo is loopback-only. Phone/LAN access needs a token and suitable network
protection. Never put tokens in URLs or browser persistent storage.

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Service health, not hardware readiness. |
| `GET /status` | `{dog,perception,mode,pending_checkin,integrations}`; missing observations are null. |
| `GET /map` | Latest map, or 404 before initialization. |
| `GET /events?since=0` | Event array strictly newer than `since`, ordered by timestamp; clients deduplicate by event ID. |
| `POST /events/{id}/ack` | `{by:"family"}` → updated event. Idempotent receipt, not proof of resident safety. |
| `POST /say` | `{text}` → command ID and queued status; not a playback confirmation. |
| `GET /commands` | Recent queued commands. |
| `POST /commands` | `{cmd,waypoint?}` → queued command; reject unknown waypoints. |
| `POST /query` | `{text}` → `{answer,answerable,citations:[{frame_id,ts,pose,crop_url}]}`. Local lexical retrieval initially; missing evidence is explicitly unanswerable. |
| `GET /frames/{id}` | Only an approved stored crop, otherwise 404; never arbitrary files or full frames. |
| `POST /ingest` | `{channel,data}` → validated ingestion. |
| `WS /live` | Send `{token}` first if authentication is configured. Receive `{type:"snapshot",data:status}`, then `{type:channel,data:payload}`. No frame bytes. |
| `POST /demo/seed` | Explicitly seed synthetic home state. Demo mode only. |
| `POST /demo/scenario` | `{scenario}` with safe_bed/fall/help/okay/timeout. Exercises the same policy engine. Demo mode only. |
| `POST /agents/run` | Optional `{task,evidence,allow_cloud:true}` advisory team; requires enabled configuration and valid bounded text evidence. |

`POST /reminders` remains stretch and is not implemented. Invalid payloads
return 422, absent resources 404, unauthorized access 401/403, and disabled
optional providers a clear 503. Reconnect clients refetch state and events;
the WebSocket itself is not a durable queue. A `resync` envelope means refetch
status/map/events/commands. Overlap the last millisecond when using `since`
and deduplicate by ID, or refetch all events; timestamps are not unique cursors.

The implemented WebSocket update types are `dog.status`, `dog.map`,
`brain.perception`, `voice.heard`, `event`, `command`, and `checkin` (pending
state or null), plus `resync`. The logical `brain.event`, `dog.cmd`, and
`voice.say` channels above describe adapter destinations; app clients consume
the implemented `event` and `command` envelopes.

## Incident state machine

1. Two distinct, consecutive, fresh observations of a present person lying on
   known floor/chair, confidence ≥0.8, start one `fall_suspected` check-in.
   Bed, unknown location, low confidence, duplicate/stale/future input, and
   inference failures cannot start an incident.
2. Queue "Grandma, are you OK?" and start an eight-second window. Correlate
   speech to the incident; pre-incident or unrelated speech cannot clear it.
3. Confident explicit reassurance emits `checkin_ok`. Explicit help or timeout
   emits one `fall_confirmed` escalation. Ambiguous speech leaves the timer open.
4. Debounce continuing incidents until a safe new observation resets the episode.
   A background clock expires windows even when no new frame arrives.

`fall_confirmed` means **escalation confirmed**, not a medically verified fall.
The conservative known-floor/chair rule intentionally does not apply the
source's literal `location != bed` to unknown locations. Thresholds and timeout
are demo settings, not validated clinical performance. Voice failure must
remain distinguishable from a successful check-in as adapters are connected.

## Privacy and evidence

The source says full frames never leave GX10 but also routes body frames via
a laptop and permits an alert crop. The coherent policy is: **full frames stay
on the trusted local robot/compute network; only explicitly released derivatives
may leave**. Crops, captions, and transcripts can still contain PII.

Cloud Deepgram/ElevenLabs, Elastic, Linq, or Subconscious are external egress,
so this is local-first, not fully air-gapped. The demo uses synthetic data and
may return null crop URLs; it does not fabricate live evidence. A cloud VLM
fallback would require a separate policy change.

Subconscious agents are advisory only: no dog-control, emergency-policy,
notification, or tool-execution authority. Provider errors cannot change the
deterministic incident rules. External requests are explicit and bounded.

## Integration signoff

Rehearse bed exemption, unknown/low-confidence posture, duplicates/stale ticks,
reassurance, help, timeout, acknowledgement, reconnect, and unanswerable queries.
Separately verify robot movement, model inference, actual speech playback,
notifications, Elastic retrieval, and measured demo latency before claiming
them complete. A map pin labels observer position, not resident position.
