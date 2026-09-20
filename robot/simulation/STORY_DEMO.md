# Annie finds Janine

## The story

Zach cannot reach his mother, Janine. His messages have not been delivered for
two days, and he suspects her phone needs charging. He texts Annie, the robot
in Janine's home, to find her and pass on his request.

Annie searches the house using its camera, remembered observations, and measured
movement receipts. After finding a person, it says that Zach wants Janine to
plug in her phone and check his messages. The supplied names are conversation
context; Annie does not identify Janine from her face.

Janine replies: “I lost my phone.” Annie retrieves a prior observation of a
smartphone on the sitting-room chair and speaks what it last saw. It should say
“I last saw it on the chair,” rather than claim it knows the phone's current
location or that it witnessed who placed it there.

The essential demo is **Zach's text → model-selected search → delivered speech**.
The memory reply is the second beat. There is no fall or emergency in this story.

## Frontend

The main simulator offers a **Zach → Annie** message composer. Sending it uses
`POST /agent/start` with a goal containing Zach's text and instructions to find
Janine from camera evidence, choose the search route, speak the request, and
finish after audio playback. No destination or resident coordinates are supplied.

**Janine's reply** is an explicit demo-actor text input. It starts another goal
using `/control`, asking the model to retrieve phone observations, speak a
grounded answer, admit missing evidence, and finish after playback. It is not
microphone input. Wispr Flow can be used as ordinary dictation into that text
field; the demo does not claim a Wispr API integration.

The UI shows model decisions separately from accepted commands, measured
completion, generated audio, and completed playback. Historical memories retain
capture IDs, timestamps, and observer poses.

Both story submissions include `require_speech: true`. The simulator records
speech command IDs with the issuing map and goal revision. A generated clip,
failed playback, missing receipt, or speech from an older goal cannot complete
this delivery requirement. Observation-only custom goals can omit it. This
checks execution; the model still chooses its route, words, and actions.

## Phone-memory prelude

The house contains a life-size smartphone on the chair. Before Zach's message,
an explicit setup camera captures it. This is a historical scene prelude, not
a claimed robot journey. Its observer pose comes from MuJoCo camera transforms.
The real image model captions the rendered image; Graphiti stores that result.
Object metadata and a handwritten expected answer never enter inference.

After loading the updated house and letting scene loading finish:

```sh
.cache/dimos/.venv/bin/python -m robot.simulation.phone_memory_prelude
# Inspect the saved JPEG. This next step uses the configured inference budget.
.cache/dimos/.venv/bin/python -m robot.simulation.phone_memory_prelude --index
```

`output/phone-memory-prelude.json` records the setup camera, model-file digest,
actual image path, capture identity, model caption, and indexing receipt. A
scene/map change requires a new capture. Selecting a scene never creates a
phone memory automatically.

On 2026-09-19, Gemini 3.8 Flash captioned the phone image as a wooden armchair
with dark cushions and a smartphone standing on the seat. Inference took
1.859 seconds and Graphiti acknowledged the original capture. This verifies
the prelude; a full story run must separately verify search and delivery.

## Measured live run, 2026-09-19

Both story forms were submitted through the live frontend. Annie selected the
living-room waypoint from the initial entrance camera, walked there under the
trained locomotion policy, and received a measured arrival receipt. Its exact
saved camera frame shows Janine seated on the chair. The model generated:
“Hi Janine, Zach asked me to remind you to plug in your phone and check his messages.”
The 4.36-second native audio clip completed playback before goal completion.

The operator then submitted “I lost my phone.” Graphiti retrieved the original
phone prelude capture, and Annie spoke a historical chair location. That
10.36-second clip also completed playback before the second goal finished.
The first answer read a Unix timestamp aloud; the planner now receives derived
memory ages and instructions to speak natural relative times. Exact timestamps
and capture IDs remain in evidence.

| Measurement | Result |
| --- | --- |
| Zach: first frame → goal completion | 21.714 s |
| Janine: first frame → goal completion | 23.758 s |
| Image/model calls across both goals | 8 |
| Provider median / range | 1.532 s / 1.435–2.074 s |
| Memory provider | Local Graphiti, connected |
| Measured simulation speed | Approximately 1.00× |

The operator's pause between the two goals is excluded from those durations.
The raw record is `output/live-zach-janine-story.json`; exact accepted frames
are retained with matching perception metadata under `output/story-*.jpg` and
`.json`. Independent frontend checks saved `output/story-ui-zach-observation.json`
and `output/story-ui-janine-observation.json`. These results establish local
device playback, not that a human heard or acknowledged the message. The reply
was typed by the demo actor; the phone prelude remains an explicit setup capture.

### Repeat after playback and speech-time fixes

A fresh house/map and newly inferred phone prelude repeated both frontend
submissions with `require_speech: true`. Both goals finished with
`speech_delivery: completed`. Annie chose the living room, received a measured
arrival, and its retained camera frame shows Janine seated. The phone reply was:
“I last saw a phone resting on the living room armchair less than a minute ago.”
Graphiti returned a robot-camera phone observation captured 27 seconds before
the reply's inference, along with the original setup-camera citation. The exact
reply camera also shows the phone against the chair cushion.

- Zach: **21.779 s** from first frame to completion; native clip **4.31 s**.
- Janine: **11.606 s** from first frame to completion; native clip **3.46 s**.
- **7** model calls; median **1.827 s**, range **1.427–2.489 s**.
- Both native playback processes completed before their respective goals.
- Evidence: `output/live-zach-janine-verified-delivery.json`, with exact speaking
  frames and metadata at `output/verified-story-*.jpg` and `.json`.

Verification for this milestone: **452 Python tests passed, 1 skipped** in the
software integration suite; **24 MuJoCo scene/camera tests** and **8 frontend
regression tests** passed. Schema export, JavaScript syntax, and diff checks
passed. The skipped embedded graph test requires the isolated Graphiti runtime;
actual local Graphiti indexing/retrieval was exercised in both live runs.

## Acceptance

1. Zach's submitted text creates a new goal revision and appears in the UI.
2. The model selects search actions from real rendered images; the dog actually
   changes position and receives measured navigation receipts.
3. The model reports a person from image evidence and generates Zach's message.
4. The speech clip completes playback before the goal can finish.
5. Janine's lost-phone reply retrieves the indexed phone frame on the current
   map; the answer cites historical evidence and does not invent identity,
   placement, or current presence.
6. Missing memory produces an honest inability to answer. A stalled provider,
   unavailable graph, or exhausted budget is visible and cannot count as success.

Accepted synthetic inference frames are retained in the bounded
`.data/simulation/evidence/` cache with matching metadata. This cache is not a
clinical or surveillance retention system. See [intelligence](INTELLIGENCE.md),
[local graph memory](LOCAL_MEMORY.md), and [the earlier measured run](LIVE_INTELLIGENCE_RUN.md).

## Physical input and output

The GX10 is the logical service host. The current localhost services stand in
for it; synthetic-image inference currently uses a cloud model.

| Endpoint | Sends to the service | Receives from the service |
| --- | --- | --- |
| Zach's family app | Text request | Delivery and task receipts |
| Janine's phone | Typed text now; microphone audio in the voice extension | Reply text and playable audio; playback acknowledgment returns to the service |
| Robot | Camera frames, LiDAR, pose, battery and controller status | Bounded movement commands; execution receipts return to the service |

For the phone voice extension, the path is phone microphone → transcription →
brain and memory → text-to-speech → phone speaker. It does not require routing
raw microphone audio through the robot. “Come here” needs a known room or a
visible-person target; a voice packet by itself does not locate the speaker.
The current text story and native simulator speech do not establish this future
phone microphone transport or physical robot voice playback.

The separately developed [Annie Audio iPhone app](../../speaker/mic/README.md)
provides a network microphone/speaker transport: mono 16 kHz PCM over WebSocket.
Its current binary protocol does not itself perform transcription or reasoning,
and its transport verification is separate from this text-driven story.
