# Live integration evidence

## One-command local showcases

From the repository root, with the existing app/brain `.venv`, simulator
`.cache/dimos/.venv`, scene catalog, locomotion policy and YOLO assets prepared
as described in [simulation setup](../robot/simulation/README.md):

```sh
.venv/bin/python robot/demo.py
```

Keep that terminal open. The launcher starts the app on `:8000`, the viewer on
`:8766`, the local brain on `:8004`, and one agent bridge. It waits for app and
brain `/health`, viewer `/state`, the `grandmas-house` scene and person detector, and a fresh bridge
status for that map. The viewer uses locomotion, advisory person detection and
native audio. It prints `http://127.0.0.1:8000/app/` and
`http://127.0.0.1:8766/`. Ctrl-C stops its process groups, including on partial
startup failure. An occupied service port is refused with its PID; existing
services are never stopped. Logs go under `.data/simulation/demo-logs/`.

The brain uses local Ollama `qwen3-vl:2b-instruct` at `:11434`; Ollama and that
model must already be installed and running. Health readiness does not prove
model inference latency or recognition quality. Root `.env` supplies auth;
launcher overrides demo mode, requires playback receipts, and disables hosted
memory. It enables no cloud fallback. Planning remains paused until requested.
Do not use the viewer's bridge-start/full-house buttons alongside this launcher.

In a second terminal, run one showcase at a time:

```sh
.venv/bin/python robot/showcase.py status
.venv/bin/python robot/showcase.py patrol
.venv/bin/python robot/showcase.py fall
.venv/bin/python robot/showcase.py tricks
```

- `status` prints app status, viewer navigation, brain health, and bridge file
  age/errors even if another component is unreachable; unavailable/stale status
  returns a nonzero exit code.
- `patrol` pauses autonomous planning, settles previous motion, then visits
  living-room, bedroom, hallway and home. Each next step waits for the previous
  command's identified completed receipt. A failed receipt stops the sequence.
- `fall` requires a fresh local agent bridge, resets only a resolved demo
  episode, stages the resident fall, and enables local camera-driven planning.
  It prints the new incident's `fall_suspected`, its `say` command, verified
  playback/reply window and escalation, with measured elapsed times. It does
  not inject perception, speech receipts or replies. With no microphone input,
  this backend reports `checkin_audio_failed: input_unavailable` before family
  attention; this is not evidence that a resident stayed silent. Playback failure
  or missing timeline stages fail the showcase. Planning is paused afterward.
- `tricks` sequences spin, circle, zigzag, wiggle and figure8, waiting on each
  receipt. An older app's HTTP 422 prints `trick command not available yet` for
  each trick; other errors fail. This operator showcase is distinct from the
  planner's instruction to reserve tricks for celebrating a reassured resident.

Every step prints elapsed seconds. `--timeout 180` is the default per receipt or
incident timeline; increase it explicitly if desired. The launcher has a
separate `--timeout 90` readiness deadline. Neither script changes Wi-Fi or
sends physical robot commands. Simulator person detection remains advisory;
missing detector results still inhibit movement. Local Qwen has previously
missed the five-second action/evidence freshness limit, so a full local fall
showcase remains an acceptance task, not a promised result.

### This implementation session: actual checks and limits

- Ran `.venv/bin/python robot/demo.py`: correctly refused existing listeners
  `:8000 PID 96964`, `:8766 PID 21128`, `:8004 PID 70260`; no child started.
- Ran `.venv/bin/python robot/showcase.py status`: **0.003 s total**, reported
  `ConnectError` for all three HTTP services in this sandbox, and read a fresh
  bridge status file (age **0.15 s**). This is a failure-path measurement,
  not a working-stack or inference latency result.
- App backend suite: **185 passed in 2.38 s**. Focused planner/context/bridge/
  execution/continuous-brain/showcase suite: **78 passed in 0.60 s**. Launcher
  lifecycle suite: **9 passed in 0.14 s**. Combined focused and launcher run:
  **87 passed in 0.73 s**. These checks mock HTTP and child
  processes; they do not establish live rendering, playback or model quality.
- Schema export `--check`, family `robot/frontend/app.js` syntax, and simulator
  `robot/simulation/web/app.js` syntax passed.
- A broader simulator run in `.cache/dimos/.venv` recorded **313 passed,
  6 failed, 1 skipped in 24.64 s**. Failures were a concurrently changed
  speech-receipt mock signature (subsequently passed in the focused run), four
  hosted-memory CA tests (including absent `trustme`), and native `say` producing
  a zero-duration WAV in this environment. An initial combined run in `.venv`
  also lacked MuJoCo and hit sandbox multiprocessing restrictions; it is not
  simulator qualification.
- Live patrol/fall/tricks and fresh-stack startup were **not measured**:
  listeners already existed and the coding sandbox denied localhost HTTP.
  Git fetch and staging were also denied by the read-only `.git` boundary;
  these changes could not be committed or pushed from this session.

## Repeatable full-house run, 2026-09-19

Two complete runs passed on the same Mac: **41.864 s / 13 image inferences**
and **37.974 s / 8 image inferences**. The configured model was
`google/gemini-2.5-flash-lite:floor`, explicitly using synthetic camera frames.
The model chose the living-room waypoint; trained-policy joint actuation moved
the dog over 0.5 m before two distinct floor-lying observations initiated a
check-in. Saved camera images were visually inspected and show the resident
lying on the floor. No authored posture labels were sent to the model.

Native question playback completed before the reply window opened. Local
Whisper transcribed the recorded synthetic reply as “Not okay.” (286.6 ms and
262.9 ms respectively); both replies applied to their identified incidents.
The family alert persisted, and the family message completed native playback.
These are playback-process receipts, not human-witnessed speaker audibility.
The routine and fall are staged, and the reply is a recorded fixture. Robot
intent comes from the model. This does not establish real microphone capture,
physical robot motion, or external notification delivery.

The second run used the explicit demo-only episode reset, which preserves
alerts, memory and command history and rejects an active check-in. The viewer
owns its command bridge; the AI button starts it and Pause AI pauses planning while retaining voice delivery. Person
detections are advisory in this simulator configuration; missing detector
results still hold movement. Physical Go2 controls are unchanged.

Reports and image evidence remain in ignored `output/house-demo-*.json` and
`output/house-frame-*.jpg`; latest report is `.data/simulation/house-demo.json`
(run `4d7b5ea8-348c-4cd2-8258-89b3bb25462e`). The combined backend, planner,
bridge, audio and demo-lifecycle suite passed **204 tests**; schema export and
both frontend JavaScript syntax checks passed. See the
[reproduction commands](../robot/simulation/README.md#full-house-model-driven-demonstration).

Local Qwen planning repeatedly took 7–10 s on this machine and its actions were
correctly rejected by the five-second freshness boundary. The verified demo
explicitly uses the cloud service, with no automatic provider fallback, at most
20 image attempts per run, and a $19 shared reservation cap plus the earlier
$1 probe reservation within the authorized $20 total. This small repeat sample
is not the full scenario matrix or 100-attempt latency qualification.

## Earlier integration run

Development run on 2026-09-19, Apple M1 Max / 64 GB. This is a measured partial
integration result, not a declaration that every [acceptance gate](ACCEPTANCE.md)
passes. Evidence: ignored `output/live-loop-gemini.jsonl` and `output/vision/`.
Run used commit `e95696f` plus the then-uncommitted person-stop, navigation,
UI, and delivery-policy changes; it is not the final frozen qualification run.

## Observed live chain

1. `floor_lying-000`, seed 2026, direct MuJoCo at 1×. The trained Go1 policy
   turned the robot toward the resident; no base-pose teleport.
2. YOLO11s on actual robot-camera pixels detected the person (example score
   0.789) and inhibited motion. The interrupted turn has a failed receipt
   naming the person stop. Local detector latency was 84–106 ms in this run.
3. Explicit `google/gemini-2.5-flash-lite:floor` inference identified a person
   lying on the floor. Seven admitted captures reached the app/WebSocket in
   1,043–1,499 ms; nearest-rank p95 is 1,499 ms for **n=7**, not the required
   100-attempt qualification. Capture IDs, wall timestamps and observer poses
   were preserved; authored labels were not sent to the model.
4. Second evidence created one `fall_suspected` at Unix ms `1789851104436` and
   one question command `10f392d5-c485-4f53-9dfb-b97b575c8d11`.
5. The enabled simulator browser played the WAV and reported `ended`;
   completed command receipt arrived at `1789851111487`. That starts the
   eight-second response window. This proves browser-reported completion,
   not human-witnessed speaker audibility or mute detection.
6. The no-reply timer produced one family-attention event at `1789851119517`.
   A connected WebSocket received it 5 ms later. The real family browser
   displayed the alert and completed speech receipt. Visible UI latency was
   not instrumented. No SMS, phone call, or external notification was sent.

Cold question synthesis made request-to-completion about 7.05 s, exceeding the
6 s target. The approved fixed question is now cached: measured preparation
5,296 ms cold, **0.8 ms cached**, with a 2.66 s WAV. A new live run must validate
the resulting startup/completion targets. Browser-only output remains an
explicit demo setup.

## Model findings

- Local Qwen3-VL 2B can be fast on a warm, reduced frame, but incorrectly called
  one floor view a bed at 0.95 confidence. It is available as an explicit local
  option; it is not qualified for this incident demo.
- YOLO11n missed that feet-first view. YOLO11s detected both saved viewpoints
  after correcting the RGB/BGR input convention; no score threshold was
  lowered to conceal the miss. This remains a finite-view prototype.
- Local Whisper tiny.en transcribed synthetic okay/help/negated-okay clips in
  120–124 ms; a 5.27 s introduction took 209 ms warm. Raw log-probability and
  no-speech signals are retained, not converted to invented confidence.
- MiMo transcribed the introduction correctly after its required audio data-URI
  prefix was added. It took about 7.7 s; it remains an explicit cloud option.

## Remaining acceptance work

Recorded synthetic replies now bind to live simulator incidents as recorded above.
The earlier timeout demonstrates a software no-reply branch, not a healthy
microphone observing resident silence. Real input availability, echo handling,
multi-player leases, full scenario/repetition matrices, and 100-attempt latency
qualification remain open. Physical patrol, GX10 deployment, full DimOS, Elastic,
and external notification delivery have not been demonstrated. See [deployment target](DEPLOYMENT_TARGET.md).

External-call reservations at this run: $2.38 shared ledger plus $1 reserved
for the earlier fast-model probe, below the $20 total allowance. Reservations
are conservative bounds, not actual billed spend; incomplete provider accounting
prevents claiming an exact total. The current runtime uses a $19 shared cap to
leave room for that separate $1 reservation. Local perception/STT/TTS has no
per-call API charge.

## Physical dog demo (one command on the Mac)

Dog on the phone hotspot (`172.20.10.10`), Mac USB-tethered to the phone, AirPods as the
Mac's default audio. Then:

```sh
robot/demo_dog.sh
```

It starts `app_backend` on `:8000` (bound to the Mac's hotspot address so the phone app can
post messages), the errand brain on `:8010`, and the dog process on `:8011` (idle: explore and
greet; on a message: find the person, speak, listen, report). Command center at
`http://127.0.0.1:8011/` (camera with boxes, remembered LiDAR world with fading history and the
walked floor draggable into 3D, pipeline latency, brain/voice/memory log, mission receipts,
buttons; `/telemetry.json` feeds it), 4D time scrub at `/spacetime`, the old page at `/classic`.
Without the dog: `.cache/dimos/.venv/bin/python robot/dog/view/replay.py --file
.data/hardware/spacetime.jsonl --port 8012 --speed 3` serves the same page from a recording
(no video; the camera panel shows a placeholder). Ctrl-C stops everything and
sends StopMove. Voice uses ElevenLabs/Deepgram when the keys are in `.env`, else local.
Elastic indexing of sightings: `.venv/bin/python robot/go2_spacetime_elastic.py --tail` with
the loopback node from `docker run ... elasticsearch:8.15.0` (see `robot/go2_spacetime_elastic.py`).
Verified 2026-09-20 in software end to end and on the dog for the idle patrol; the app mission
on hardware is the first thing to run once the dog is charged.
