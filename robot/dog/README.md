# `robot/dog` — Annie's dog stack

One package, one dog process, one place per concern. Maps onto the team whiteboard
(2026-09-20): the family app talks to `app_backend`; the **agent** (mission
coordinator + patrol brain) runs on the Mac or the DGX; the **robot** is a hand and
an arm reached through one command + video contract; the **memory** is a
timestamped, positioned diary the agent queries before it decides.

| Package | What lives there | Was |
| --- | --- | --- |
| `link/` | WebRTC link helpers: probe, walk, tricks, follow controller | `go2_probe/walk/tricks/follow` |
| `perception/` | threaded convert→track→annotate pipeline, red-shirt target id, person re-identification (`reid.py`: face → clothing signature → numbered guests, `.data/faces/guests.json`), people directory, object detection, hardware perception loop | `go2_perception_pipeline`, `go2_target_id`, `go2_perception` |
| `planning/` | smart patrol (LiDAR sectors, stall, bandit, occupancy grid), the patrol brain context/decision, the mission board | `go2_smart_patrol`, `go2_patrol_brain`, `go2_missions` |
| `missions/` | the family-message coordinator (`errand`), dimOS skills | `go2_errand`, `dimos_skills` |
| `voice/` | cloud voice (ElevenLabs/Deepgram with local fallback), wake-word commands, host voice loop, mic/speaker selection by name (`devices.py`) incl. the `speaker/mic` iPhone app as `iPhone (Annie Audio)` over WebSocket :8030 (`phone_audio.py`) | `go2_voice_*`, `go2_host_voice` |
| `memory/` | space-time recorder + JSON API, Elasticsearch indexer, the 4D graph | `go2_spacetime`, `go2_spacetime_elastic` |
| `view/` | command-center page (`command_center.html`, served at `/` by the dog process), the 4D viewer, `replay.py` (page from a recording), vendored Three.js | — |
| `runtime/` | the dog process (`patrol`: explore/greet idle, missions on request, live view) and the standalone body service | `go2_patrol_greet`, `go2_body` |
| `inference.py` | the single place a language/vision model is called (provider switch) — owned by Henry | `annie_inference` |

Old paths (`robot/go2_*.py`, `robot/dimos_skills.py`) are shims that import the new
modules, so launchers, docs and tests keep working; new code imports `robot.dog.…`.

Rates, for orientation: perception every frame (~14 fps, ~40 ms); LiDAR sectors per
map (~5/s); control tick 15 Hz; brain decision every ~4 s while idle; recorder 5 Hz;
missions on demand. Guardrails (collision, stall, battery, leash, stale link) sit in
`runtime/patrol.py` and always win over the brain and the app.

For the hardware-free full-stack demo and its live HTTP checks, see
[SIM_DOG.md](../../docs/SIM_DOG.md). Install the additional test dependencies in
the backend environment before running the dog suites:

```bash
uv pip install --python .venv/bin/python -r robot/requirements-test.txt
.venv/bin/python -m pytest robot/tests -q
```

The rendered simulator uses the separate dimOS environment documented in
[simulation/README.md](../simulation/README.md). It does not prove physical gait,
contact handling, onboard audio, or hardware stop behavior.
