# Robot workspace

All simulator/demo code and its contract live here. The top-level
`app_frontend/` Swift app and `app_backend/` / `robot_backend/` team scaffolds
are independent. No simulator runtime is imported by those scaffolds.

```text
robot/
  simulation/       MuJoCo, scene factory, locomotion, perception, speech
    web/            Simulator viewer UI (localhost:8766)
  frontend/         Family demo UI (localhost:8000/app/)
  app_backend/      Demo API, incident policy, event/command journal, memory
  robot_backend/    Image/audio brain and simulator-side service
  contract/         Demo API contract, schemas, exporter
  go2_probe.py      Physical connection diagnostic (separate from simulation)
  host_check.py     Hardware host inventory
```

Run commands from the **repository root**. Existing `.env`, `.cache`, `.data`,
and `output` stay in their ignored root locations; no models, credentials or
runtime databases are moved into tracked code.

```sh
.venv/bin/uvicorn robot.app_backend.app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
.cache/dimos/.venv/bin/python robot/simulation/viewer.py \
  --model .cache/menagerie/unitree_go2/scene.xml \
  --scenes .data/simulation/scenes/manifest.json --locomotion --person-safety --port 8766
```

Use separate terminals. See [simulator setup](simulation/README.md),
[demo API](app_backend/README.md), [brain contract](contract/brain.md), and
[physical hardware setup](SETUP.md). Image inference is separately configured;
starting the renderer does not make a paid model call.

## Physical robot — Henry

Henry owns the Go2 Air, DimOS connection, and simulation-to-hardware transition.
This is the `body` workstream from the original plan.

## Scope

- Bring up the simulated Go2, then the physical Air using the same interfaces.
- Publish camera frames with frame IDs, capture timestamps, map IDs, and matching
  robot/camera poses to Roger's local perception pipeline.
- Supply map, waypoint, connection, battery, and movement status when available.
- Execute goto, pause/resume, look, and software-stop requests. Report accepted,
  completed, and failed states only when supported by observed SDK behavior.
- Own networking, hardware setup, and the physical emergency-stop procedure.

Keep SDK bring-up, blueprints, waypoint configuration, and hardware assets here.
Scene fixtures and repeatable simulator checks belong in
[simulation](simulation/SPEC.md). Coordinate edits in
`robot/robot_backend/app/hardware/` with Roger: Henry owns device behavior and Roger
owns the service-side integration. No firmware modification is assumed.

## Handoffs

| Partner | Henry supplies | Henry receives |
| --- | --- | --- |
| Roger — robot backend | Local frames, poses, maps/status, command outcomes | Movement requests and perception-input requirements |
| Ellis — app backend | Approved device status through Roger | User commands through Roger |
| Sam — frontend | Verified movement/status semantics through the services | Demo interaction requirements |

Follow the [local integration contract](contract/README.md) and
[external signal boundary](../shared/README.md). Rich local observations are not
automatically approved for app-server/cloud delivery. Robot pose is the
observation position, not a measured resident position.

## First milestone

1. Start/reset the simulated Go2 and obtain a rendered camera frame.
2. Deliver the frame and matching capture-time pose to Roger.
3. Reach one waypoint, then three; verify arrival from simulator state.
4. Verify pause/resume, software stop, and honest disconnected/error status.
5. Replace the simulator connection with the Air and repeat the interface checks.

Use the [simulation acceptance matrix](simulation/SPEC.md). A supplied status
fixture does not prove movement. Record SDK/model versions, run mode, launch
command, and observed outcomes for each milestone.

## Hardware and setup

Team hardware: Go2 Air, ASUS GX10, and DJI microphones. The Air's onboard audio
is unverified; coordinate an external audio path with Roger. Verify that the
GX10 model accepts an actual image. Add tested host/dependency/network/launcher
instructions here once bring-up succeeds; keep local caches and credentials
out of Git.

Own `robot/`; coordinate shared schema and adapter changes before editing another
owner's files. Follow the repository [merge rules](../AGENTS.md).
