# Annie

Annie connects an elderly resident living alone with their family, through a
robot dog. A relative sends a message from their phone; the dog finds the
resident, delivers it, listens to the reply, and answers questions from what it
has actually observed around the house. Separately, a possible-incident
check-in escalates to the family when reassurance does not arrive.

## The two halves

Annie is two independent services that talk over the LAN, so the resident's
home and the family's phone can be in different places.

```text
  Family (phone / browser)                Resident's home
  ┌────────────────────────┐              ┌──────────────────────────────┐
  │ app_frontend  (SwiftUI)│              │ robot/  errand brain,        │
  │ frontend      (web)    │              │         Go2 control, sim     │
  └───────────┬────────────┘              └───────────────┬──────────────┘
              │ REST + WebSocket                          │
        ┌─────┴──────────┐  POST /dispatch ───────────────┘
        │  app_backend   │ ◄─ POST /internal/events ───────
        │  + MongoDB     │
        └────────────────┘
```

| Path | What it is | State |
| --- | --- | --- |
| `app_backend/` | Family-facing API: messages, runs, reminders, observation memory, incident policy | **Working**, 86 tests |
| `app_frontend/` | SwiftUI app for iPhone and Mac | **Working** on device and simulator |
| `frontend/` | Phone-friendly web app served at `/app/` | **Working** |
| `robot/` | The robot side: errand brain serving `/dispatch`, physical Go2 control, perception, simulator | **Working**, see [robot/README.md](robot/README.md) |
| `robot_backend/` | Original top-level placeholder; the working robot service lives under `robot/` | Unused scaffold |
| `shared/`, `contract/` | Protocol references and exported typed schemas | — |

## What is real, and what is staged

Latest integration handoff: [phone delivery, Pause, screenshots, and open
acceptance gaps](docs/PHONE_DELIVERY.md). The updated native screen displayed a
24.909-second simulated reminder delivery with mocked audio, and the physical
family Pause path returned a software-stop acknowledgment in 129.7 ms. Physical
voice/movement qualification and four failing fall scenarios remain open; this
is not a production signoff.

Being precise about this matters more than the feature list.

**Real:** the async message path (a message is accepted in ~30 ms and the
robot's errand is reported afterwards, so the app never blocks on the dog);
run events streaming to phone and browser over WebSocket; MongoDB persistence
of the household schema; the incident state machine; the simulator driving an
actual Go2 model with a trained walking policy, camera-driven perception, and
execution receipts.

The message boundary is implemented on both sides: `robot/dog/missions/errand.py`
serves `/dispatch` and reports back through `/internal/events`, and
`robot/demo_dog.sh` brings up the app, the errand brain and the dog together.
The first app-to-dog missions ran on the physical Go2 on 2026-09-20 — it found
the resident, spoke, and relayed her reply. See [docs/TODO.md](docs/TODO.md)
for the recorded runs and their open items.

**Staged or unconnected:** observation memory is seeded with synthetic data,
and the resident's spoken replies have been transcribed only in fragments so
far. Full DimOS, Linq delivery, and Elastic remain unconnected. For working on
the app alone, `app_backend/scripts/fake_robot.py` stands in for the robot and
prints everything the two sides exchange.

`fall_confirmed` means **escalation confirmed**, never a medically verified
fall. Queued, acknowledged, and executed are distinct states throughout.

## Run the family app

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r app_backend/requirements.lock
.venv/bin/uvicorn app_backend.app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open [the web app](http://127.0.0.1:8000/app/). `GET /api/storage` reports
whether records are persisting to MongoDB or the in-process fallback, so
"is it saving?" is never a guess.

To watch the whole message path with no robot and no GX10, run the stand-in in
a second terminal and point the backend at it:

```sh
.venv/bin/python app_backend/scripts/fake_robot.py --auto-reply
```

It prints every payload `app_backend` sends and posts the callbacks back.
`ANNIE_FAMILY_MOCK_ROBOT=true` is a second option that skips HTTP entirely.
See [app_backend/README.md](app_backend/README.md) for every route and
environment variable, and [contract/family_messages.md](contract/family_messages.md)
for the robot-side interface.

The iPhone app is in [app_frontend/](app_frontend/README.swift). On a phone it
needs the Mac's LAN address, an `ANNIE_API_TOKEN`, and that address in
`ANNIE_ALLOWED_HOSTS`; the simulator needs none of these because it shares the
Mac's network.

## Run the robot and simulator

The simulator workspace is isolated under [robot/](robot/README.md) and runs
independently of the family app. The [MuJoCo viewer](robot/simulation/README.md)
serves [localhost:8766](http://127.0.0.1:8766/) with the actual Go2 model, live
rendering, and camera views. The scene factory supplies repeatable furnished
homes. With `--locomotion`, a matched Go1 model and trained DimOS policy walk
the environment using an authored collision map and waypoint planner — a Go1
simulation surrogate, not a validated Go2 hardware controller. The
[HTTP bridge](robot/simulation/README.md) forwards app commands, publishes
simulated poses, and records execution receipts.

Local YOLO inhibits movement when it detects a person. Speech uses local
synthesis with browser playback receipts; local Whisper and explicit MiMo
transcription accept synthetic WAVs. See
[measured demo evidence](docs/LIVE_DEMO.md), [acceptance targets](docs/ACCEPTANCE.md),
and [SDK findings](docs/SIMULATION_FINDINGS.md).

## Data boundary

Full resident frames stay on the trusted local robot/compute network; only
explicitly released derivatives leave it. `app_backend` performs **no**
inference and holds no model-provider credentials — all of it happens on the
robot side, on-device. Crops, captions, and transcripts can still contain
personal information, so this is **local-first, not air-gapped**. Cloud voice,
memory, notification, and advisory paths each require explicit configuration.
The current demo uses synthetic data only.

## Verification

```sh
# Family app: 86 tests, no database or network required
PYTHONPATH=app_backend .venv/bin/python -m pytest app_backend/tests -q
.venv/bin/python contract/export_schemas.py --check
node --check frontend/app.js

# Robot and simulator
.venv/bin/python -m pytest robot/app_backend/tests robot/robot_backend/tests -q
```

The family-app tests run against an in-memory store and mocked transport; two
additional tests exercise a real MongoDB and skip when none is listening.
Dependencies are pinned in `app_backend/requirements.lock`. Simulator and
physics tests have separate prerequisites and do not establish VLM or hardware
performance.

## Documentation

| File | Purpose |
| --- | --- |
| [SPEC.md](SPEC.md) | Product scope and acceptance criteria |
| [contract/family_messages.md](contract/family_messages.md) | app_backend ↔ robot_backend interface, with a robot-side handoff |
| [app_backend/README.md](app_backend/README.md) | Every route, environment variable, and the household schema |
| [robot/contract/README.md](robot/contract/README.md) | Channel, event, and privacy semantics |
| [robot/simulation/SPEC.md](robot/simulation/SPEC.md) | SDK/simulation proposal and scenario matrix |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Choices and rationale |
| [docs/TODO.md](docs/TODO.md) | Current work and integration gates |
| [docs/LIVE_DEMO.md](docs/LIVE_DEMO.md) | Measured demo evidence |
| [AGENTS.md](AGENTS.md) | Shared contributor instructions (`CLAUDE.md` symlinks here) |

## Prior art

- [ReMEmbR](https://github.com/NVIDIA-AI-IOT/remembr): robot memory joining visual captions, capture time, and observer pose; the main reference for Annie's observation-memory design.
- [Embodied-RAG](https://arxiv.org/abs/2409.18313): spatial retrieval over non-parametric embodied memory.
- [Meta-Memory](https://arxiv.org/abs/2509.20754): semantic-spatial memory retrieval for robot spatial reasoning.
- [Enter the Mind Palace](https://proceedings.mlr.press/v305/ginting25a.html): episodic scene representations for long-term embodied question answering; CoRL 2025, with an earlier RSS workshop version.
- [Go2 Pro eldercare via WebRTC](https://link.springer.com/chapter/10.1007/978-3-032-29254-4_11): related work on offloading perception to an edge node for home assistance.
- [DimOS](https://github.com/dimensionalOS/dimos): Annie reuses its matched Go1 walking policy in direct MuJoCo; full SDK integration has a separate acceptance gate.

Annie combines ReMEmbR-style observation memory, edge-hosted perception, and a
family-facing app. The current implementation runs locally with a Go1
simulation surrogate; GX10 deployment is the target.
[Prior-art details](docs/PRIOR_ART.md) distinguish reused components,
implemented design ideas, and research references.
