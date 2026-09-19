# Annie

Annie connects an elderly resident at home with family through a robot dog.
The first workflow is a possible-incident check-in, two-way communication,
and scene memory with cited evidence.

## Architecture

The simulator/demo workspace is isolated under [robot/](robot/README.md).
The top-level `app_frontend/` Swift app and `app_backend/` / `robot_backend/`
team service scaffolds remain independent.

```text
Family app <-> App API + SQLite <-> Robot service <-> DimOS / Go2 / local vision
                    |                    |
             Optional advisory      Voice and approved
                agent team          notification adapters
```

- `robot/app_backend/`: working local API, event policy for the demo, memory, commands,
  and optional Subconscious advisory team.
- `robot/robot_backend/`: independent robot-side service, bounded image/audio inference,
  and hardware-adapter scaffolding.
- `robot/frontend/`: phone-friendly web interface served at `/app/`.
- `shared/` and `robot/contract/`: protocol references and exported typed schemas.
- `robot/simulation/`: SDK exploration, scenario specification, and physics checks.

The simulator sends actual robot-camera images to a configured vision model,
publishes measured poses, and executes waypoint/turn/stop commands. Local YOLO
inhibits movement when it detects a person. Speech uses local synthesis and
browser playback receipts; local Whisper and explicit MiMo transcription accept
synthetic WAVs. The family web app receives camera-driven incident events.
Full DimOS, physical Go2/GX10, Linq delivery, and Elastic remain unconnected.
See [measured demo evidence](docs/LIVE_DEMO.md) and [acceptance targets](docs/ACCEPTANCE.md).

## Run the local software demo

From the repository root:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r robot/app_backend/requirements.lock
.venv/bin/uvicorn robot.app_backend.app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open [the local app](http://127.0.0.1:8000/app/). The backend starts empty; use
**Start simulated home** to load clearly labeled synthetic observations.
The app service health endpoint is `/health`; authenticated OpenAPI is
`/openapi.json`. See [app backend details](robot/app_backend/README.md).

For root `.env` settings append `--env-file .env`. Use [.env.example](.env.example)
as a reference without replacing existing keys. Phone/LAN access requires an
API token and the intended host in `ANNIE_ALLOWED_HOSTS`.
[Subconscious setup](docs/SUBCONSCIOUS.md) documents the opt-in text advisory team;
no paid calls or notifications run automatically.

The separate [robot backend](robot/robot_backend/README.md) runs on port 8001 during
local development. Its hardware packages remain scaffolds. A separate [brain service](robot/contract/brain.md) accepts rendered JPEGs through a configurable local/cloud vision endpoint.

## Run the live robot simulator

The separate [MuJoCo viewer](robot/simulation/README.md) runs at
[localhost:8766](http://127.0.0.1:8766/) with the actual Go2 model, live rendering,
play/pause, reset, single-step, camera views, and optional joint-pose holding.
The scene factory supplies repeatable furnished homes, with bulk generation and measured playback timing. With `--locomotion`, a matched Go1 model and trained DimOS policy walk through the environment using an authored collision map and waypoint planner. This is a Go1 simulation surrogate, not a validated Go2 hardware controller. The [HTTP bridge](robot/simulation/README.md) forwards app commands, publishes actual simulated poses, and records execution receipts. [SDK findings](docs/SIMULATION_FINDINGS.md) record actual
launch results and remaining integration work.

## Contract and data boundary

The team explicitly expanded the initial status-only proposal on 2026-09-19.
The app-facing contract may carry status, map positions, captions, events,
released evidence crops, transcripts needed for check-ins, and two-way commands.
Consumers still validate exact schemas; this is not permission for arbitrary
unbounded payloads or secrets. The legacy `RobotSignal` remains a status-only
reference; the richer v0.1 models are in [robot/contract/](robot/contract/README.md).

Full resident frames remain on the trusted local robot/compute network. Cloud
voice, memory, notifications, or advisory requests have explicit configuration
and data-egress paths. Crops/captions can contain PII. This is **local-first**,
not fully air-gapped, and the current demo uses synthetic data only.

## Documentation

| File | Purpose |
| --- | --- |
| [SPEC.md](SPEC.md) | Current product scope and acceptance criteria. |
| [robot/contract/README.md](robot/contract/README.md) | API, channel, event, and privacy semantics. |
| [robot/simulation/SPEC.md](robot/simulation/SPEC.md) | Full SDK/simulation proposal and scenario matrix. |
| [docs/TODO.md](docs/TODO.md) | Current work and integration gates. |
| [docs/BRAINSTORM.md](docs/BRAINSTORM.md) | Ideas and alternatives. |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Choices and rationale. |
| [docs/hackmit-2026/NOTES.md](docs/hackmit-2026/NOTES.md) | All supplied team planning notes and sources. |
| [docs/hackmit-2026/SPONSORS.md](docs/hackmit-2026/SPONSORS.md) | Sponsor resources, links, codes, and uncertainties. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Development workflow and skills. |
| [AGENTS.md](AGENTS.md) | Shared agent instructions; `CLAUDE.md` is a relative symlink. |

## Verification

```sh
.venv/bin/python -m pytest robot/app_backend/tests -q
.venv/bin/python robot/contract/export_schemas.py --check
node --check robot/frontend/app.js
```

Tests use mocked services and synthetic data. Dependencies are pinned in
`robot/app_backend/requirements.lock`; review updates deliberately. SDK/physics tests
have separate prerequisites and do not establish VLM or hardware performance.

Keep ideas in the brainstorm, chosen behavior in the spec, work in TODO, and
rationale in decisions. Push coherent verified milestones frequently.

## Prior art

- [ReMEmbR](https://github.com/NVIDIA-AI-IOT/remembr): robot memory joining visual captions, capture time, and observer pose; the main reference for Annie's observation-memory design.
- [Embodied-RAG](https://arxiv.org/abs/2409.18313): spatial retrieval over non-parametric embodied memory.
- [Meta-Memory](https://arxiv.org/abs/2509.20754): semantic-spatial memory retrieval for robot spatial reasoning.
- [Enter the Mind Palace](https://proceedings.mlr.press/v305/ginting25a.html): episodic scene representations for long-term embodied question answering; CoRL 2025, with an earlier RSS workshop version.
- [Go2 Pro eldercare via WebRTC](https://link.springer.com/chapter/10.1007/978-3-032-29254-4_11): related work on offloading perception to an edge node for home assistance.
- [DimOS](https://github.com/dimensionalOS/dimos): Annie reuses its matched Go1 walking policy in direct MuJoCo; full SDK integration has a separate acceptance gate.

Annie's target combines ReMEmbR-style observation memory, edge-hosted perception,
and a family-facing app. The current implementation runs locally with a Go1
simulation surrogate; GX10 deployment is a target. [Prior-art details](docs/PRIOR_ART.md)
distinguish reused components, implemented design ideas, and research references.
