# Annie decisions

Record consequential product and technical choices, including why they were
made. Current behavior belongs in `../SPEC.md`; work status belongs in `TODO.md`.

## DEC-001: Shared documentation and agent instructions

- Date: 2026-09-19.
- Status: Adopted as the initial repository convention; revisable.
- Context: The repository needs a documentation convention shared by contributors and coding agents.
- Decision: Keep product scope in `SPEC.md`, actionable work in `docs/TODO.md`, exploratory ideas in `docs/BRAINSTORM.md`, and rationale here. Keep shared agent rules in `AGENTS.md`, with `CLAUDE.md` as a relative symlink.
- Reason: Each document has one purpose, and both agent entry points use the same maintained instructions.
- Tradeoff: Symlink support is needed in each checkout. For Windows contributors without it, replace the link with a regular `CLAUDE.md` containing `@AGENTS.md`.

## DEC-002: Contract-first local demo

- Date: 2026-09-19.
- Status: Adopted for the initial software milestone.
- Context: The team needs parallel app, backend, brain, and body work before hardware and sponsor APIs are provisioned.
- Decision: Keep the existing FastAPI backend; use typed schemas, SQLite, an in-process event bus, and a phone-friendly web app for deterministic local scenarios. Preserve Redis and service adapters as explicit integration work.
- Reason: The contract and check-in behavior can be tested without keys or a robot, while teammates build against stable payloads.
- Consequences: One server worker; pending check-ins and queued commands are transient. The local demo does not prove physical patrol, perception accuracy, speech playback, or notification delivery.

## DEC-003: Local-first observations and optional advisory agents

- Date: 2026-09-19.
- Status: Adopted implementation boundary.
- Context: Notes request local PII preprocessing, cloud speech/memory/messaging, and optional Subconscious multi-agent support.
- Decision: Keep full frames on the local robot/compute network. Optional cloud adapters have explicit egress and configuration. Subconscious receives only explicitly supplied bounded text evidence and cannot control the robot, decide emergency escalation, or send notifications.
- Reason: Cloud services are incompatible with an end-to-end air-gap claim; uncertainty and action authority need clear boundaries.
- Consequences: Crops, captions, and transcripts still need a data policy. The initial Subconscious path is tested with mock HTTP, not paid live calls. The deterministic incident policy operates independently.

## DEC-004: Separate services with a richer app contract

- Date: 2026-09-19.
- Status: Adopted following explicit user clarification.
- Context: Teammates introduced independent `app_backend/` and `robot_backend/` services with an initial fixed-status-only external envelope. The user explicitly allowed the broader maps, captions, evidence, and communication contract.
- Decision: Retain the two-service layout and move the working demo API into `app_backend/`. Permit typed maps, observer poses, captions, events, approved crops, check-in transcripts, and commands across the app boundary. Keep the old `RobotSignal` for compatibility, not as the entire protocol.
- Reason: Parallel owners need independent services while the app needs evidence and two-way communication to demonstrate Annie's workflow.
- Consequences: The robot transport and execution receipts remain open integration work. Full frames remain on the trusted local body/brain network. Rich payloads still require authentication, validation, and explicit release/retention rules; changing the contract does not connect adapters automatically.

## DEC-006: Async family message dispatch as a separate in-memory feature

- Date: 2026-09-19.
- Status: Adopted for the HackMIT demo; revisable.
- Context: The team wants Zach to send free-text messages from the iPhone app
  that the dog delivers to Jeanine and relays a reply back, without blocking
  the app on a 60-90 second physical sequence, and without a GX10 available
  for every test.
- Decision: Add `POST /api/messages`, `GET /api/runs/{run_id}`, `GET /api/thread`,
  `WS /ws/family`, and `POST /internal/events` to `app_backend`, entirely
  in-memory (no SQLite, no auth beyond a hardcoded three-person household plus
  the existing bearer/loopback boundary). Document the new app_backend-to-
  robot_backend interface in `contract/family_messages.md`; `robot_backend` is
  untouched. An `ANNIE_FAMILY_MOCK_ROBOT` flag drives a canned event sequence
  for demoing without the GX10.
- Alternatives: Extend the existing SQLite `Service`/incident state machine
  instead of a parallel in-memory model; rejected because the two features
  have unrelated failure modes (safety check-in vs. free-text relay) and the
  explicit product ask was in-memory-only for this path.
- Consequences: Messages and runs do not survive an app_backend restart. The
  `/dispatch` and `/internal/events` contract has no implementation yet on the
  `robot_backend` side; until that lands, real dispatch reports `unreachable`,
  which is the intended fail-open behavior, not a bug.

## DEC-007: Native iPhone build of the SwiftUI companion app

- Date: 2026-09-19.
- Status: Adopted following explicit user choice; revisable.
- Context: The SwiftUI companion in `app_frontend/` was macOS-only and talks to the Swift mock backend in `app_frontend/backend/`, not the FastAPI `app_backend/`. The user wanted it usable on an iPhone.
- Decision: Build the same `app_frontend/Sources/AnnieApp` code for iOS through a hand-written `Annie.xcodeproj`, alongside the SwiftPM Mac target. The phone reaches the backend over the home Wi-Fi at an address entered in the Profile tab and stored on the device. Signing team and bundle ID stay in a gitignored `Local.xcconfig`.
- Alternatives: A phone-friendly web page served by the backend (no signing, works on Android, but a second UI to keep in sync); both.
- Reason: One codebase for Mac and iPhone; no machine-specific IPs or account settings in tracked files.
- Consequences: The backend is plain HTTP with no authentication, so `iOS/Info.plist` allows local-network HTTP and anyone on the same network can reach it. Free-account installs expire after 7 days. Verified by an iOS simulator build and run against the live backend; not yet verified on a physical iPhone. This app does not use the `app_backend/` contract; unifying the two is open work.

## DEC-008: Profile registration UI first; MongoDB storage and creation rules deferred

- Date: 2026-09-19.
- Status: Adopted following explicit user direction; revisable.
- Context: The user wants app-user and dog-user profiles tracked, including which was created first, with an app user's registration also creating the dog user's profile, stored in MongoDB. The backend owner may have specific needs, and no MongoDB or driver exists in the Swift mock backend.
- Decision: Build only the SwiftUI registration screens now. An app user registers and creates both profiles, linked; a dog user can register alone. Profiles are stored on the device (`ProfileStore`), with each profile's creation time and links recorded so "created first" is derivable. Do not add MongoDB, a profile API, or backend changes yet.
- Alternatives: A separate Python profile service, or profiles inside `app_backend/`, with a local MongoDB; deferred until the backend owner weighs in.
- Reason: Avoids choosing backend ownership, storage, and a wire contract on the backend owner's behalf; keeps `app_frontend/backend/` untouched.
- Consequences: Profiles exist only on one device, so they are not shared or recoverable and a reinstall clears them. Undecided: what happens when a dog profile is created first, whether several app users can share a dog, and how profiles are paired across devices. The `Profile` JSON shape is provisional, not a backend contract. Tracked as TASK-007.

## DEC-009: One app with two audiences, served by one backend

- Date: 2026-09-19.
- Status: Adopted for the HackMIT demo; revisable.
- Context: The demo needs a family-facing surface where Zach writes to Annie
  and watches the errand, but `app_frontend` was entirely resident-facing and
  pointed at the standalone Swift mock server, which does not implement the
  family message endpoints.
- Decision: Add a top-level audience picker to the SwiftUI app (Grandma view
  and Family view) over one shared `AppState`, and point the whole app at
  `app_backend`. To make that possible, `app_backend` now also serves the
  resident view's `/api/reminders`, `/api/memory` and `/api/ask` using the
  Swift client's existing wire shapes. Run events carry backend-derived
  `summary` and `speaker` fields so the client renders text rather than
  interpreting robot-supplied payloads.
- Alternatives: Point each view at a different server (two addresses to
  configure on demo day, twice the failure surface); build the family surface
  in the `frontend/` web app instead (faster, but the user asked for the
  phone app).
- Consequences: One address to type into the phone, and the dog's `recalled`
  line cites the same observation the resident's activity feed shows. The
  Swift mock backend in `app_frontend/backend/` is now redundant for this
  path and is left in place rather than removed. Because `app_backend` refuses
  non-loopback clients without a token, a physical phone needs
  `ANNIE_API_TOKEN` set and entered in Profile; the simulator does not.

## DEC-010: All-local deployment profile, including Elastic

- Date: 2026-09-19.
- Context: The user requested a sponsor-ready environment based on the working stack, then explicitly selected everything local, including Elastic.
- Decision: Reuse local Ollama vision, Whisper/macOS speech, SQLite, and Graphiti. Support self-hosted Elasticsearch with its own API key and CA certificate; keep Graphiti selected until local Elastic is provisioned and verified. Cloud voice, advisory agents, and messaging are not dependencies of this profile.
- Consequences: Prior cloud story results do not establish all-local acceptance. Elastic replaces the bridge memory provider, not the app journal. The user expects MongoDB to work; a fresh pull still contains no MongoDB driver or connection setting, so verify the team's integration before asserting its status. See [the environment guide](LOCAL_ENV.md).

## Future entries

Use the next `DEC-NNN` ID. Include date, status, context, decision, alternatives,
and consequences. If a decision changes, add a new entry and mark the older one
superseded with a link. Link implementation details instead of duplicating them.


## DEC-005 — Isolate the simulator workspace under robot (2026-09-19)

- Context: The user explicitly requested that all simulator-related code, including its frontend, app backend, robot backend, and contract, live under `robot/`.
- Decision: Move the working simulator/demo stack to `robot/{simulation,frontend,app_backend,robot_backend,contract}` and use qualified `robot.*` Python imports. Keep the existing top-level team health scaffolds and Swift app independent. This supersedes DEC-003's placement of the demo in the top-level app backend.
- Consequences: Run commands from the repository root using the new paths. Local URLs remain port 8766 for the simulator and port 8000 for the family demo. Root `.env`, ignored caches, datasets, and runtime databases retain their locations. The relocated demo has one incident engine; no second policy engine was added.
