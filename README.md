# Annie

Annie connects an elderly resident at home with family through a robot dog.
The first workflow is a possible-incident check-in, two-way communication,
and scene memory with cited evidence.

## Architecture

```text
Family app <-> App API + SQLite <-> Robot service <-> DimOS / Go2 / local vision
                    |                    |
             Optional advisory      Voice and approved
                agent team          notification adapters
```

- `app_backend/`: working local API, event policy for the demo, memory, commands,
  and optional Subconscious advisory team.
- `robot_backend/`: independent robot-side service; currently health plus
  package scaffolding for hardware, vision, voice, communication, and privacy.
- `frontend/`: phone-friendly web interface served at `/app/`.
- `shared/` and `contract/`: protocol references and exported typed schemas.
- `simulation/`: SDK exploration, scenario specification, and physics checks.

The app and robot services remain independently runnable. The proposed robot
WebSocket transport, hardware control, local VLM, voice execution, Linq, and
Elastic adapters are not connected yet. The current app workflow uses synthetic
inputs and an in-process bus; queued commands do not prove execution.

## Run the local software demo

From the repository root:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r app_backend/requirements.lock
.venv/bin/uvicorn app_backend.app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open [the local app](http://127.0.0.1:8000/app/). The backend starts empty; use
**Start simulated home** to load clearly labeled synthetic observations.
The app service health endpoint is `/health`; authenticated OpenAPI is
`/openapi.json`. See [app backend details](app_backend/README.md).

For root `.env` settings append `--env-file .env`. Use [.env.example](.env.example)
as a reference without replacing existing keys. Phone/LAN access requires an
API token and the intended host in `ANNIE_ALLOWED_HOSTS`.
[Subconscious setup](docs/SUBCONSCIOUS.md) documents the opt-in text advisory team;
no paid calls or notifications run automatically.

The separate [robot backend](robot_backend/README.md) runs on port 8001 during
local development. Its feature packages are scaffolds, not working integrations.

## Run the live robot simulator

The separate [MuJoCo viewer](simulation/README.md) runs at
[localhost:8766](http://127.0.0.1:8766/) with the actual Go2 model, live rendering,
play/pause, reset, single-step, camera views, and optional joint-pose holding.
Its README includes pinned dependencies and model setup for a fresh checkout.
It is currently separate from DimOS navigation and the family app's synthetic
incident scenarios. [SDK findings](docs/SIMULATION_FINDINGS.md) record actual
launch results and remaining integration work.

## Contract and data boundary

The team explicitly expanded the initial status-only proposal on 2026-09-19.
The app-facing contract may carry status, map positions, captions, events,
released evidence crops, transcripts needed for check-ins, and two-way commands.
Consumers still validate exact schemas; this is not permission for arbitrary
unbounded payloads or secrets. The legacy `RobotSignal` remains a status-only
reference; the richer v0.1 models are in [contract/](contract/README.md).

Full resident frames remain on the trusted local robot/compute network. Cloud
voice, memory, notifications, or advisory requests have explicit configuration
and data-egress paths. Crops/captions can contain PII. This is **local-first**,
not fully air-gapped, and the current demo uses synthetic data only.

## Documentation

| File | Purpose |
| --- | --- |
| [SPEC.md](SPEC.md) | Current product scope and acceptance criteria. |
| [contract/README.md](contract/README.md) | API, channel, event, and privacy semantics. |
| [simulation/SPEC.md](simulation/SPEC.md) | Full SDK/simulation proposal and scenario matrix. |
| [docs/TODO.md](docs/TODO.md) | Current work and integration gates. |
| [docs/BRAINSTORM.md](docs/BRAINSTORM.md) | Ideas and alternatives. |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Choices and rationale. |
| [docs/hackmit-2026/NOTES.md](docs/hackmit-2026/NOTES.md) | All supplied team planning notes and sources. |
| [docs/hackmit-2026/SPONSORS.md](docs/hackmit-2026/SPONSORS.md) | Sponsor resources, links, codes, and uncertainties. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Development workflow and skills. |
| [AGENTS.md](AGENTS.md) | Shared agent instructions; `CLAUDE.md` is a relative symlink. |

## Verification

```sh
PYTHONPATH=app_backend .venv/bin/python -m pytest app_backend/tests -q
.venv/bin/python contract/export_schemas.py --check
node --check frontend/app.js
```

Tests use mocked services and synthetic data. Dependencies are pinned in
`app_backend/requirements.lock`; review updates deliberately. SDK/physics tests
have separate prerequisites and do not establish VLM or hardware performance.

Keep ideas in the brainstorm, chosen behavior in the spec, work in TODO, and
rationale in decisions. Push coherent verified milestones frequently.
