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

## DEC-005: Native iPhone build of the SwiftUI companion app

- Date: 2026-09-19.
- Status: Adopted following explicit user choice; revisable.
- Context: The SwiftUI companion in `app_frontend/` was macOS-only and talks to the Swift mock backend in `app_frontend/backend/`, not the FastAPI `app_backend/`. The user wanted it usable on an iPhone.
- Decision: Build the same `app_frontend/Sources/AnnieApp` code for iOS through a hand-written `Annie.xcodeproj`, alongside the SwiftPM Mac target. The phone reaches the backend over the home Wi-Fi at an address entered in the Profile tab and stored on the device. Signing team and bundle ID stay in a gitignored `Local.xcconfig`.
- Alternatives: A phone-friendly web page served by the backend (no signing, works on Android, but a second UI to keep in sync); both.
- Reason: One codebase for Mac and iPhone; no machine-specific IPs or account settings in tracked files.
- Consequences: The backend is plain HTTP with no authentication, so `iOS/Info.plist` allows local-network HTTP and anyone on the same network can reach it. Free-account installs expire after 7 days. Verified by an iOS simulator build and run against the live backend; not yet verified on a physical iPhone. This app does not use the `app_backend/` contract; unifying the two is open work.

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

## Future entries

Use the next `DEC-NNN` ID. Include date, status, context, decision, alternatives,
and consequences. If a decision changes, add a new entry and mark the older one
superseded with a link. Link implementation details instead of duplicating them.
