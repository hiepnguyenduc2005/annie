# Body commands — brain to physical Go2 (command + video, draft v0.1)

Added 2026-09-19. The dog is a hand and an arm: nothing on it thinks. Whatever
brain runs off-robot (the GX10, the Mac, Claude behind the phone app) sends one
succinct command at a time to the **body service** and looks through its camera.
Implementation: `robot/go2_body.py` (runs on the machine that holds the WebRTC
link to the dog; today the Mac tethered to the phone hotspot). Tests:
`robot/tests/test_go2_body.py`.

## Transport

HTTP/JSON on the trusted local network, default `0.0.0.0:8001`. When
`ANNIE_BODY_TOKEN` is set on the body host every request must carry
`X-Body-Token: <token>` (exact match) or it is 401. Raw camera frames never
leave this LAN; a brain that needs cloud inference sends its own derived
observations, not `/frame.jpg`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status` | `link` (connected/connecting/disconnected), `battery_soc_percent`, `telemetry_fresh`, `pose {x,y,yaw}` (odometry, metres/radians), `ranges_m {front,left,right}` from the LiDAR voxel map (null = clear/unknown), `frame_age_ms`, `people` (current tracker output incl. `identity` when a face is enrolled), `command` (the executing receipt or null) |
| GET | `/frame.jpg` | latest camera frame as JPEG (≤640 px wide); 404 before the first frame |
| POST | `/command` | `{"command_id"?: str, "name": str, "args": {...}}` → **202** receipt; the same `command_id` again → **200** current receipt (idempotent); another command while one is executing → **409** `{"error": "busy", "current": receipt}`; bad name/args → **400** `{"error": reason}` |
| GET | `/command/{command_id}` | the receipt; 404 for an unknown id (the body keeps the last 200) |
| POST | `/stop` | cancel the executing command (its state becomes `cancelled`) and send a priority StopMove → `{"stop_code", "ack_ms", "note"}` |

Receipt: `{"command_id", "name", "args", "state", "accepted_at_ms", "started_at_ms"?,
"finished_at_ms"?, "progress"?, "result": {...}|null, "error": str|null, "stop_code"?}`.
`state` is `accepted → executing → completed | failed | cancelled`. Poll
`GET /command/{id}` every ~0.5 s; every command has a bounded duration.

## Vocabulary

| `name` | `args` | `result` | Notes |
| --- | --- | --- | --- |
| `stand`, `sit`, `hello`, `stretch`, `heart`, `dance` | none | `{"codes": {...}, "note"}` | Firmware sport-mode ids. Tricks other than stand/sit send StandUp + BalanceStand first. Codes are acknowledgments; `"no_ack"` means the request was sent but the firmware did not answer within 8 s (long behaviours). |
| `stop` | none | `{"stop_code", "ack_ms"}` | Always accepted, even while busy; runs synchronously and returns 200. |
| `move` | `vx` m/s in [-0.2, 0.4], `wz` rad/s in [-0.8, 0.8], `duration_s` in [0.1, 10] | `{"vx","wz","duration_s"}` | Streams Move at 10 Hz, then zero velocity + StopMove. |
| `patrol` | `duration_s` in [1, 300] | `{"collisions", "modes", "distance_from_origin_m"}` | Smart patrol: LiDAR sector ranges + odometry stall → cruise/blocked/backoff/homing (`robot/go2_smart_patrol.py`). |
| `find_person` | `name` str or null, `timeout_s` in [1, 120] (default 60), `approach` bool (default true) | `{"found", "track_id", "identity", "matched_name", "approached", "searched_s"}` | Wanders (with collision handling) until an upright person is tracked; prefers a face match for `name` when enrolled, otherwise the closest person (`matched_name: false`). With `approach`, walks toward them until the box is close. |
| `say` | `text` 1–300 chars | `{"played", "where": "host speaker"}` | Blocks until playback ends. The dog has no verified speaker; audio plays on the body host. |
| `listen` | `max_s` in [1, 15] (default 8) | `{"transcript", "heard", "speech_ms", "where": "host microphone"}` | Host mic → Silero VAD → local Whisper. A transcript is recognised text, not understanding. |

## Refusals (receipt `state: failed`, human-readable `error`)

Motion commands are refused, without moving, when the link is not connected,
telemetry is older than 1.5 s, the battery is under the 40 % floor, or the
operator's motion-inhibit marker exists. A running motion command also stops
itself the moment any of those becomes true. `say`/`listen` need no link.

## Safety wording

Stops are software stops through the same link; a lost link cannot stop the
robot from the brain. Acknowledgments and receipts are not evidence that the
motion happened; the pose in `/status` is the odometry estimate.

## Example: Claude (or the GX10 planner) delivering a message

```
POST /command {"name": "find_person", "args": {"name": "Jeanine", "timeout_s": 90}}
GET  /command/<id>   ... until completed → result.found
POST /command {"name": "say", "args": {"text": "Jeanine, it's Annie. Zach asked me to pass this along: ..."}}
POST /command {"name": "listen", "args": {"max_s": 10}}   → result.transcript
```

`robot/go2_errand.py` is exactly this sequence, driven by
`contract/family_messages.md` dispatches and reporting events back to
`app_backend`.
