# Live integration evidence

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
