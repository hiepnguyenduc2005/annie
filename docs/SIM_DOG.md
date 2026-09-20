# Simulated dog stack (offline full-stack demo)

One launcher brings up the whole product loop with the physical dog off, audio
mocked, and every provider path disabled. It is for the family-app demo, the
iPhone pointing at localhost, and reproducible end-to-end verification.

## Run it

    .venv/bin/python robot/demo_sim.py

Defaults: family app http://127.0.0.1:8120 (loopback, no token; web UI at
/app/), errand http://127.0.0.1:8110, simulated dog http://127.0.0.1:8111
(command center; source=simulation, mock audio with the deterministic resident
reply "okay thank you"). Options: --check (preflight only),
--app-port/--errand-port/--dog-port, --sim seated|floor|empty, --duration,
--ready-timeout, --sim-root. Requires the repo .venv (backend deps) and
.cache/dimos/.venv (MuJoCo); nothing is installed automatically.

Ctrl-C stops exactly the three launched processes. If any child dies the others
are stopped, the failed child's log tail is printed, and the launcher exits
non-zero. Occupied ports fail fast with the listener PID.

## What is isolated

- No .env is sourced. Provider credentials, cloud adapters, hardware secrets
  and persistence variables inherited from the shell are stripped (including
  any *_API_KEY / *_TOKEN shaped names).
- ANNIE_LLM_PROVIDER=off (no inference network), ANNIE_VOICE_CLOUD=0, cloud
  agents off; MongoDB is off and the app uses its in-memory fallback.
- One fresh internal secret per run; the family API accepts loopback clients
  without a token.
- All data lives under one unique root (.data/sim/stack-<timestamp>/):
  SQLite, face index (ANNIE_FACES_DIR), sightings/spacetime JSONL, and logs.

## Probe a running stack

    .venv/bin/python robot/verify_sim.py

Asserts, in order: all three health endpoints; dog telemetry and app
/api/dog/status report source=simulation and /voice reports mocked audio
BEFORE any command is sent; if any of those checks fail the probe exits 1
without POSTing anything. It then runs a say mission via POST /command
(expecting result.source="simulation", where="simulation mock") and a family
relay ("tell Grandma the simulator check is complete") through
POST /api/messages, asserting navigating/arrived/speaking/listening/heard/
completed events and the mocked transcript "okay thank you". Waits are
bounded; failures are reported honestly (a find_person mission can fail when
rendered-YOLO detection misses the seated mannequin; the probe reports that
failure rather than a false pass).

## Unit tests

    .venv/bin/python -m pytest robot/tests/test_demo_sim.py -q

Covers env sanitization, port/duration validation, preflight failures
(occupied port, missing venvs), readiness timeouts, and process-group cleanup.

## Scene setup and evidence limits

Follow [the simulator setup](../robot/simulation/README.md) for the dimOS
environment and pinned Go2 menagerie model. Then fetch/build the apartment as
described in [asset provenance](../robot/simulation/assets/apartment/SOURCES.md).
The committed source manifest records the asset URLs and hashes used in this run.

This dog uses kinematic motion in a furnished MuJoCo scene, a collision raster,
rendered camera frames, and the actual image tracker. It does not run a trained
Go2 gait. Its geometric voxel feed is not a calibrated physical LiDAR simulation.
The mocked audio never accesses speakers or microphones. Separate virtual story
tests use synthetic detections and a deterministic clock; those test controller
behavior, not image-recognition accuracy.

## Verified demo and remaining failures (20 September 2026)

The iPhone 17 Pro Simulator connected to this stack and visibly completed a
family reminder from the Reminders screen: find Jeanine, deliver “charge your
phone,” listen, and display “okay thank you.” The real HTTP run took 29.8 seconds;
camera perception was rendered, while speech and the resident reply were mocked.

After the final reminder/Pause backend and paused-telemetry changes, a fresh
stack passed all 19 live HTTP checks in `robot/verify_sim.py`. Its family run
`29db0ae9-25b3-483b-adfe-b0a18c7f7018` recorded navigation, arrival, speaking,
listening, the mocked reply, and completion. The full robot suites passed
545 tests (5 skipped) in `.venv` and 550 tests (1 skipped) in the dimOS
environment before the final small telemetry/probe fixes; those fixes then
passed 7 focused runtime tests and 28 launcher/probe tests in each environment
for the latter suite. Contract exports and JavaScript syntax checks passed.

The repeatable virtual sweep is:

```bash
.cache/dimos/.venv/bin/python robot/tests/story_sweep.py \
  --story both --seeds 20 --workers 2 --out /tmp/annie-stories.json
```

The message story passed 20/20 seeds. The fall story passed 16/20 with no forced
virtual-clock timeouts. Seeds 1, 5, and 8 asked twice after losing and reacquiring an
unnamed resident; seed 15 contacted a synthetic person while following a
bystander. The geometry estimate changes with viewpoint and cannot establish a
stable unnamed identity in all these cases. These failures remain reproducible
and the sweep correctly exits nonzero. Do not describe this as autonomous
production qualification. Physical collision clearance, moving stop, link loss,
real audio, and door contact still need their own supervised acceptance.
