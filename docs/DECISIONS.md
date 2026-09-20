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
- Status: Adopted following explicit user direction; the app-user/dog-user split itself is superseded by [DEC-011](#dec-011-family-only-app-resident-view-removed) (no more dog-user profile), but the on-device-storage-first approach still stands.
- Context: The user wants app-user and dog-user profiles tracked, including which was created first, with an app user's registration also creating the dog user's profile, stored in MongoDB. The backend owner may have specific needs, and no MongoDB or driver exists in the Swift mock backend.
- Decision: Build only the SwiftUI registration screens now. An app user registers and creates both profiles, linked; a dog user can register alone. Profiles are stored on the device (`ProfileStore`), with each profile's creation time and links recorded so "created first" is derivable. Do not add MongoDB, a profile API, or backend changes yet.
- Alternatives: A separate Python profile service, or profiles inside `app_backend/`, with a local MongoDB; deferred until the backend owner weighs in.
- Reason: Avoids choosing backend ownership, storage, and a wire contract on the backend owner's behalf; keeps `app_frontend/backend/` untouched.
- Consequences: Profiles exist only on one device, so they are not shared or recoverable and a reinstall clears them. Undecided: what happens when a dog profile is created first, whether several app users can share a dog, and how profiles are paired across devices. The `Profile` JSON shape is provisional, not a backend contract. Tracked as TASK-007.

## DEC-009: One app with two audiences, served by one backend

- Date: 2026-09-19.
- Status: Superseded by [DEC-011](#dec-011-family-only-app-resident-view-removed); Jeanine is not a user of this app.
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

## DEC-011: Family-only app; resident view removed

- Date: 2026-09-20.
- Status: Adopted following explicit user direction; revisable.
- Context: The user decided Jeanine should only interact with Annie in
  person, to keep that relationship humanized, not through a phone app. The
  SwiftUI app (DEC-009) had a "Grandma view" (Reminders, Ask Annie, Activity,
  Profile) and a "Family view" (Messages, Activity, Profile) behind a top-level
  audience picker. Registration (DEC-008) also let a device register as
  Annie's own "dog user" profile alongside or instead of an app user, which
  has no purpose once the app has only one audience.
- Decision: Remove the audience picker and the `Audience`/resident code path
  entirely (`AppState`, `AnnieApp.swift`). One `TabView` now shows all of
  Reminders, Ask Annie, Activity, Message Annie, and Profile to every family
  member, since `app_backend`'s reminders/memory/ask endpoints were always
  audience-agnostic (DEC-009) — only the client-side presentation
  distinguished them. Replace the app-user/dog-user registration chooser with
  a plain sign-in: pick which family member owns this phone from the fixed
  household `app_backend`'s `HOUSEHOLD` already recognizes (`zach`, `ellis`);
  that choice becomes the message `author_id`, replacing the old segmented
  "who's using this phone" picker in Profile. The dog-user profile kind is
  deleted, not deprecated — nothing else referenced it.
- Alternatives: Keep the dog-user profile as a placeholder for a future
  robot-mounted or in-person device; rejected because nothing in this app
  runs on such a device, and an unused option was part of what made the
  Profile tab look "weird and useless" per the user's own description.
  Free-text name entry at sign-in instead of picking from the fixed
  household; rejected because `app_backend`'s `MessageIn.author_id` is a
  `Literal['jeanine', 'zach', 'ellis']` — an arbitrary name would be accepted
  by sign-in and then rejected by every message send. Extending that backend
  enum to arbitrary family accounts was judged out of scope for a UI cleanup
  request and is not implemented here.
- Consequences: REQ-012 no longer describes the app (updated in SPEC.md);
  TASK-008 is narrowed to the single remaining profile kind. Sign-in is still
  on-device only (`ProfileStore`, now keyed `annieProfile`, not
  `annieProfiles`), so a reinstall or a different phone needs sign-in again;
  cross-device pairing remains open, same as DEC-008 left it. Adding a third
  family member still requires a matching `app_backend` change, since the
  household stays hardcoded on both ends. Ask Annie's copy and demo answers
  now speak about Jeanine in the third person ("Did she take her
  medication?") instead of the first person ("Did I take my medication?"),
  matching who is actually asking.

## DEC-012: Ask Annie and Message Annie merged; escalation is explicit, not inferred

- Date: 2026-09-20.
- Status: Adopted following explicit user direction; revisable.
- Context: The user wants one place in the toolbar where a family member can
  either ask Annie a question that's answered from what she's already seen
  ("did she take her medication?") or ask her to go do something in person
  ("check if the door is shut"), instead of two separate tabs. `POST
  /api/ask` (`CompanionService.ask`, `app_backend/app/companion.py`) is
  lexical recall only — instant, no robot involved. `POST /api/messages`
  (`FamilyService`, `app_backend/app/family.py`) always runs the full
  navigate/speak/listen/recall/speak errand — it physically sends the robot
  to Jeanine and has it speak to her. Nothing in the app or `app_backend`
  classifies free text into "question" vs "task"; the Subconscious advisory
  team (REQ-009) is explicitly scoped to no robot or messaging authority, so
  routing a physical dispatch through it would cross that boundary.
- Decision: Merge the "Ask Annie" and "Message Annie" tabs into one ("Ask
  Annie"). Every submission goes through the instant `/api/ask` lookup first
  and is shown as an immediate answer. If the answer is the fallback (nothing
  matched — which is also what an imperative like "check the door" or "tell
  her I'll call" gets, since neither matches anything either), that answer
  carries one explicit button, "Have Annie check with Jeanine in person,"
  which then dispatches the same text as a real message/run (REQ-010),
  reported live in the same scrolling conversation. No text is ever
  classified or auto-dispatched; the family member always makes the explicit
  second choice to physically involve the robot.
- Alternatives: Client- or server-side keyword/LLM classification to decide
  automatically whether to look up or dispatch; rejected as unreliable
  without a real intent model, and as an implicit trigger for something with
  real-world effect on Jeanine (the robot speaking to her), which the app
  should never guess into doing. An explicit "Ask" vs "Send" mode toggle on
  one composer; rejected as extra UI for the same result the fallback answer
  already signals for free.
- Consequences: A question phrased as a statement ("tell her...") always
  round-trips through an unhelpful instant "I don't know that yet" before the
  escalation button appears; this is a known rough edge, not a bug. Backend
  `MessageIn`/`CompanionService` are unchanged — this is UI-only. Ask turns
  are still client-local and not persisted (unchanged from before); a message
  and its run remain the only persisted, cross-device-visible record of Annie
  actually doing something.

## Future entries

Use the next `DEC-NNN` ID. Include date, status, context, decision, alternatives,
and consequences. If a decision changes, add a new entry and mark the older one
superseded with a link. Link implementation details instead of duplicating them.


## DEC-005 — Isolate the simulator workspace under robot (2026-09-19)

- Context: The user explicitly requested that all simulator-related code, including its frontend, app backend, robot backend, and contract, live under `robot/`.
- Decision: Move the working simulator/demo stack to `robot/{simulation,frontend,app_backend,robot_backend,contract}` and use qualified `robot.*` Python imports. Keep the existing top-level team health scaffolds and Swift app independent. This supersedes DEC-003's placement of the demo in the top-level app backend.
- Consequences: Run commands from the repository root using the new paths. Local URLs remain port 8766 for the simulator and port 8000 for the family demo. Root `.env`, ignored caches, datasets, and runtime databases retain their locations. The relocated demo has one incident engine; no second policy engine was added.
