# Annie specification

Status: Working HackMIT demo scope, 2026-09-19. Requirements describe targets,
not completed hardware or service integrations. The [team brief](docs/hackmit-2026/NOTES.md)
retains earlier ideas and source notes.

## Product and first workflow

Annie connects one elderly resident at home with family through a robot dog.
A possible incident prompts a check-in, followed by a family alert if help is
requested or reassurance does not arrive. A relative can acknowledge the alert
and send a message through the dog. This is a staged assistance prototype,
not a medical diagnosis or a replacement for emergency response.

## Version 0 requirements

| ID | Behavior | Acceptance check |
| --- | --- | --- |
| REQ-001 | One-floor map, status, and three waypoints. | App displays map ID and robot observation position; physical patrol verified separately. |
| REQ-002 | Validated timestamped perception with evidence IDs. | Invalid, stale, duplicate, unknown, timed-out, or low-confidence input cannot create an incident; bed is exempt. |
| REQ-003 | Sustained non-bed lying initiates a check-in. | Two distinct fresh floor/chair observations start one eight-second check-in; continuing ticks do not flood alerts. |
| REQ-004 | Help or timeout escalates to family attention. | Confident correlated reassurance closes the check-in; ambiguous speech does not; help or timeout escalates once. |
| REQ-005 | Two-way communication. | Dog events reach app; family messages enter identified voice commands; queue acceptance is distinct from playback/delivery. |
| REQ-006 | Evidence-backed scene memory. | Query cites frame ID, capture time, and observer pose; no evidence gives an explicitly unanswerable response. |
| REQ-007 | Full frames remain local. | Raw frames do not enter the app stream, cloud memory, cloud agents, or routine logs. Released derivatives have explicit egress policies. |
| REQ-008 | Reproducible software demo. | Fresh checkout exercises bed, incident, reassurance, timeout, acknowledgement, message, and query without keys, hardware, or outbound notifications. |
| REQ-010 | The simulated dog executes waypoint, patrol, stop/resume and look missions. | Trained-policy joint actuation moves the matched Go1 surrogate; measured poses and command receipts reach the app. |
| REQ-011 | Rendered image and synthetic audio inference use interchangeable compute services. | Preserve capture identity and timestamps, validate model outputs, report failures, and enforce the configured shared cloud budget. |
| REQ-009 | Optional Subconscious advisory team. | Bounded text-only agents run only when configured and opted in; no robot, alert, or messaging authority; provider failure is tested with mocks. |
| REQ-016 | Asynchronous family message relay to the resident, with live progress. | Posting a message returns a run ID immediately without waiting on the robot; an unreachable robot and normal progress both surface as run events, never a blocked request or a 500; connected family clients see live thread and run updates over WebSocket. |
| REQ-017 | Durable household records, and the robot's three reports back. | App users are many-to-one onto dog users; a message accumulates the robot's action summary and the resident's reply; reminder compliance and emergencies are recorded against their reminder and dog user. All three inbound routes require the shared secret and are rejected outright when it is unset. `GET /api/storage` states whether records are persisting or held in the fallback. |
| REQ-012 | SwiftUI app is family-only: Jeanine is not a user of it, and every screen (Reminders, Ask Annie, History, Profile) is shown to every signed-in family member. | Sign-in picks which family member owns this phone from the fixed household `app_backend` recognizes (`zach`, `ellis`); a second sign-in on the same device is rejected; the choice is stored on the device only and used as the message `author_id` (DEC-011, superseding the app-user/dog-user split and the resident/family audience picker in DEC-008/DEC-009). Server-side storage, cross-device pairing, and adding family members beyond the fixed household remain open. |
| REQ-015 | Ask Annie is one screen for both an instant lookup and an in-person check, and the two are never conflated. | Asking anything (`POST /api/ask`) answers immediately from recorded observations and never dispatches the robot; when nothing matches, the fallback answer is shown and a distinct, explicit "have Annie check with Jeanine in person" action is offered, which only then dispatches a run (REQ-016) with the same text (DEC-012). |
| REQ-013 | Interactive live simulation and spatial evidence. | The operator can orbit, pan, zoom, and reset the scene camera with pointer, touch, or keyboard; toggle raycast LiDAR hits, measured travel, and planned route; and distinguish those sources without changing robot-camera inference or robot motion. |

The rule uses known floor/chair locations. Unlike the source's literal
`location != bed`, unknown location requires more evidence. Confidence
thresholds are demo settings, not clinical performance. `fall_confirmed` is
retained as a wire name for escalation confirmed, never a proven medical fall.

The model-driven MuJoCo demonstration supports an explicit advisory person-detection
policy: detections inform the model's choice to approach, wait, speak, or stop;
unavailable detector results still inhibit motion. Stop enforcement remains the
default, and this option does not alter physical robot controls. Rehearsal resets
may re-arm only a resolved episode in demo mode, preserving evidence and command
history without injecting a perception observation. Full-house runs stage the
resident routine/fall and recorded reply while the model chooses robot actions.

## REQ-014: Zach's message to Janine (current demo)

Zach texts Annie because his messages to his mother, Janine, have not been
delivered. Annie must choose its search route from camera evidence, find a
person, and audibly relay Zach's request to charge/check Janine's phone. The
task completes only after measured movement and speech playback receipts.
Janine's follow-up that she lost her phone must retrieve a cited historical
phone observation and communicate uncertainty about its current location.
Names come from the supplied conversation; face identity and who placed the
phone are not inferred. This story is the immediate demonstration priority.
See [the story and acceptance checks](robot/simulation/STORY_DEMO.md).

## Scope boundaries

The current requested deployment profile runs all runtime services locally,
including Elasticsearch when selected. Use local vision, Whisper/macOS speech,
and Graphiti or self-hosted Elastic; cloud audio and advisory agents remain off.
Prior cloud-based simulator measurements remain historical evidence, not
acceptance of the all-local profile. See [local setup](docs/LOCAL_ENV.md).

Target integrations: Go2 with DimOS/MuJoCo, GX10-local image-capable Nemotron,
Deepgram, ElevenLabs, Linq, and Elastic. Track each as simulated/disconnected
until verified. Initial delivery is a phone-friendly web app and local backend;
React Native remains an option after the end-to-end workflow works.

Stretch: time-based spoken reminders and routine drift. Deferred: two floors,
multiple profiles, hospital dashboard, air-quality sensing, Arduino, robot arm,
fine-tuning, social matching, and Ansys without a specific engineering need.

## Evidence, data, and privacy

A robot pose is not a person's measured location. Preserve map ID, frame ID,
capture timestamp, and uncertainty. DimOS offers useful navigation and memory
components, but their fusion for Annie remains an integration task; captions
with coordinates do not establish persistent identity or a full 4D graph.

Full frames stay on the trusted local body/brain network. Cloud audio/text,
Elastic captions, Linq notifications, released crops, and Subconscious evidence
can contain personal information. This is local-first, not fully air-gapped.
The user authorized cloud inference for synthetic simulator frames on 2026-09-19 and subsequently raised the total external inference budget to $50. Preserve all prior spending and reservations; the shared service cap is $49 because an earlier $1 probe reservation is tracked separately. It is explicitly configured; real resident/hardware frames remain local. No automatic provider fallback is permitted.
Use synthetic data for the current demo; define retention and crop release
before collecting actual resident observations. Never put media or secrets in Git.

## Interfaces and open integration questions

[The contract](robot/contract/README.md) defines component interfaces. Resolve the
actual hardware/SDK/model versions, frame-to-pose synchronization, crop and
retention policy, named owner for brain integration, and notification/playback
acknowledgements before physical signoff. Measure demo latency and false alerts
on stated scenarios instead of inventing performance figures.

Manual Go2 control must clear held input and attempt neutral input plus a
stand-preserving StopMove before disconnect, navigation away, or loss of page
focus. Returning focus alone must not resume motion. The separate damping
control must clearly say that it relaxes the motors. Hardware commissioning
must follow the manufacturer's battery operating guidance; small odometry
changes and command acknowledgments do not establish a completed patrol.

## Detailed acceptance and codebase quality targets

[Acceptance target 1](docs/ACCEPTANCE.md) specifies component tests, exact
incident behavior, measurable performance targets, fault/restart recovery,
end-to-end scripts, and separate software/simulation/audio/notification/hardware
gates. [The architecture contract](docs/ARCHITECTURE.md) assigns owners and module
boundaries; [the run template](docs/ACCEPTANCE_RUN_TEMPLATE.md) records evidence.

These are target requirements, not assertions that the current implementation
passes. They intentionally strengthen the v0 demo: start the resident response
window after verified question playback, distinguish communication failure from
silence, persist active incidents/effects across restart, preserve the complete
evidence pair, and require affirmative recovery before re-arming an episode.
Where older prose describes current behavior differently, acceptance target 1
defines the intended next behavior. Existing strict wire contracts remain in
force until producers, consumers, schemas, and tests migrate together. See the
acceptance document's inspection baseline for the known implementation gaps.

## Local showcase operation

One repository command starts the simulator app, grandmas-house locomotion viewer
with advisory person detection, local brain and agent bridge, waits for readiness,
and tears down its child processes on Ctrl-C. Occupied service ports are refused
with listener PIDs. Separate HTTP showcase commands run the four-stop patrol,
stage a fall and observe its identified incident/playback/reply/escalation timeline,
sequence the five supported tricks by execution receipts, and summarize stack
status. Each step reports measured elapsed time and has a bounded wait. A trick
is an optional planner action for celebrating a reassured resident; execution
still uses the simulator's motion gates. These are simulator workflows only.

## Situated agent in the dog process (2026-09-20)

- Natural-language instructions reach the dog as the body command `instruct {text, author}` (from the
  command-center page, the family app via the errand, or any client of `POST /command`). The dog process
  builds a *situation* (pose, people and objects with distance/bearing/age from the spatio-temporal graph,
  under-cover state, recent greetings, memory sentences), asks the configured inference provider
  (`robot/dog/inference.py`, text only, no camera frames) for a JSON plan over the skill set
  (`find_person, say, listen, turn, walk, hello, dance, heart, stretch, sit, stand, patrol, go_home, stop`),
  validates every step against the body contract, fills in social steps the model dropped from the keyword
  rules, and runs the steps one after another as child receipts of the instruct mission. Without a model the
  keyword rules alone handle greet/tell/check/turn/walk/home/explore/sit/stand/dance/stop.
- Greetings use memory: no repeat within 5 min for the same name or the same spot (1.2 m); the line is
  composed by the model from the situation (fallback: a fixed line) and the dog lifts its nose (body pitch)
  before waving so the face is in frame. Greetings are a wave only; idle tricks are off (exploration instead).
- The dog remarks on new objects/people it just placed in the graph (rate limited, never repeats).
- `turn(degrees)` and `walk(metres)` are odometry-closed steps; `walk` stops at obstacles (< 0.45 m).
  A requested `dance` is refused when something is closer than 0.5 m ahead.
- Turn-in-place commands never go below 0.8 rad/s and reverses never below 0.2 m/s (Go2 deadbands).

## Voice, memory, people and the family app (2026-09-20, later)

- **Voice.** ElevenLabs speaks and Deepgram hears whenever their keys are present (`ANNIE_VOICE_CLOUD=0`
  or the app's Settings toggle switches to local `say`/Whisper at runtime; keys may be replaced from the
  app and live in memory only). The wake-word microphone is always on and transcribes through the same
  path. The speaker and microphone are chosen by name (`/voice` on the dog process, `/api/settings/voice`
  in the app): AirPods, the Mac's own devices, the iPhone Continuity mic, or the "iPhone (Annie Audio)"
  WebSocket app on :8030. The dog process mutes its own voice on the mic while speaking.
- **Conversation.** After a greeting (which asks how the person is) the dog listens up to 6 s, classifies
  the reply (fine / concern / other / none), answers in Annie's voice (model-composed, quality-gated) and
  for a *concern* uses fixed wording and records a `concern`. For 45 s after Annie speaks, speech needs no
  wake word: a sentence that reads like a command becomes an instruction, anything else gets a reply.
  Transcripts (`conversations`) and `concerns` are in `/telemetry.json` and `/api/dog/status`.
- **Persona.** One `PERSONA` block (caring, warm, unhurried, first names, notices how people seem, no
  machine talk) heads every plan and spoken line; fallback greetings ask how the person is.
- **Memory across restarts.** At start the dog process replays the last hour of the recorder JSONL into
  the graph (`ANNIE_MEMORY_RELOAD_S`); odometry only lines up within one power cycle.
- **People.** `/people` on the dog process and `/api/people` in the app: enrol a person from up to 10
  photos (face embeddings only are kept, in `.data/faces/index.json`), relation, shirt colour; the live
  tracker uses the new index at once. `robot/dog/perception/reid.py` re-identifies across tracker-id churn
  by face, then clothing signature, then shirt colour; unknown regulars become "Guest N" (clothing
  vectors only, no faces, expiring).
- **Missions.** Overlapping family requests queue (state `queued`, bounded at 8) instead of failing busy;
  `stop` cancels the line. The errand keeps retrying the body for 45 s while the dog process relaunches
  after a link drop.
- **Outcomes.** Every family message ends in one of three app-facing outcomes on its run: `message_response`,
  `reminder_update` (a reminder sent with `reminder_id` and acknowledged is marked done) or `emergency`
  (a reply that sounded like a call for help; listed at `/api/alerts`).
- **Family app.** Controls card with live dog status; real task chips; questions to `/api/ask` (which
  understands grandma/she as the resident and answers from the dog's live memory), tasks to missions;
  on-device speech-to-text; Settings (cloud voice, mic/speaker, keys in the Keychain, Server under
  Advanced); live History; conversations shown as an agentic timeline; People Annie knows.
- **Command center.** Served at `/` by the dog process: camera with boxes, remembered LiDAR world with a
  history slider and fading dots, latency, agent log, mission receipts, controls and an instruction box;
  `/telemetry.json` feeds it. Works from a recording (`robot/dog/view/replay.py`) and from the simulated
  dog (`--sim`, `robot/dog/sim/`) when the hardware is off.
- **Perception.** Objects come from the open-vocabulary detector (YOLO-World, folded labels such as
  `door`) when its baked checkpoint exists, warmed off the control thread; the LiDAR guard looks at
  0.10-0.75 m, stops at 0.6 m, creeps on a stale map, and a detected object filling the view counts as
  an obstacle. `look_for(thing)` scans with the camera and asks the vision model where the thing is.
- **MCP.** `robot/dog/mcp_server.py` exposes the dog (status, instruct, command, say, listen, find_person,
  look_for, where_is, people, voice settings, memory) to any MCP client; elder-care skills are
  compositions of the primitives (`robot/dog/planning/care_skills.py`).
