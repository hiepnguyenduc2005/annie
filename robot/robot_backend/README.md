# Robot backend

The approved robot-to-app boundary now includes typed maps, captions, observer
poses, event evidence, released crops, transcripts, and two-way commands. See
[contract v0.1](../contract/README.md). The older status-only envelope is a
compatibility option; full frames remain on the trusted local compute network.

**Owner: Roger.** Own local perception, agents, voice, and communication with
Ellis's app backend. Henry owns the robot connection and device behavior.

## Owner responsibilities

- Receive Henry's frames and synchronized observer poses; preserve frame ID,
  capture timestamp, and map ID through perception and memory.
- Run an image-capable local model on GX10 and validate its output. Missing
  frames, unknown/low-confidence results, and inference errors stay explicit.
- Integrate DJI/external microphone input, speech recognition, and verified
  audible playback. The Air's onboard audio is unverified.
- Send validated observations and incident-correlated replies into the existing
  local demo policy. That policy currently lives in `robot/app_backend/`; agree its
  eventual placement with Ellis instead of creating a competing state machine.
- Execute speech requests and coordinate movement requests with Henry. Report
  accepted, completed, and failed actions accurately.
- Enforce approved outbound payloads and keep raw observations local.

## Code areas and handoffs

| Area | Responsibility |
| --- | --- |
| `app/video/`, `app/agent/` | GX10 perception and local reasoning |
| `app/speech_to_text/`, `app/text_to_speech/` | Input transcription and actual playback |
| `app/hardware/` | Service-side adapter coordinated with Henry's `robot/` work |
| `app/communication/` | Authenticated signals/commands with Ellis |
| `app/privacy/`, `app/cloud/` | Explicitly approved outbound requests |

Use [contract v0.1](../contract/README.md) for approved maps, captions, poses,
transcripts, events, and commands. The [legacy status format](../../shared/messages.py)
is a compatibility option. Keep full frames local; cloud vision is not an
automatic fallback when local inference fails.

## First milestone and acceptance

1. Classify an actual rendered/camera frame on GX10 and measure latency.
2. Feed that result into Ellis's local demo and exercise bed, floor lying,
   reassurance, help, and silence scenarios.
3. Verify actual question playback and a reply tied to the correct incident.
4. Deliver one approved signal and play a family reply audibly.
5. Repeat with Henry's physical Air adapter after the simulated loop passes.

Follow the [simulation matrix](../simulation/SPEC.md). Test model timeout,
stale/duplicate frames, unknown observations, unrelated replies, and audio
failure. A supplied posture label tests policy, not vision. Keep provider calls
mocked in routine checks and record live verification separately.

Own `robot/robot_backend/`; coordinate `app/hardware/` edits with Henry and contract
changes with Ellis. Follow the repository [merge rules](../../AGENTS.md).

## Setup

This folder is an independent FastAPI service. Copy this folder to its server
and install its own dependencies. Python 3.10 or newer is recommended.

From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8001
```

Health: http://127.0.0.1:8001/health
API docs: http://127.0.0.1:8001/docs

For a server process, omit `--reload`; binding and network access depend on the
server deployment. Both servers can use port 8000 when on different hosts.
The differing development ports let both run on the same computer.

See the [project architecture](../../README.md) for responsibilities and privacy rules.
Only health endpoints are wired up; feature packages are placeholders.
