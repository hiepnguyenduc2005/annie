# Annie acceptance specification

Version: acceptance target 1, 2026-09-19.

This is a detailed target specification requested by the project owner. It is
not a report that the current implementation passes. Requirements labeled
**target change** intentionally strengthen the existing demo behavior. Implement
them through coordinated contract changes; do not send undeclared fields into
strict v0.1 consumers. Existing implementation facts are listed in section 16.

Read alongside [component architecture](ARCHITECTURE.md),
[product scope](../SPEC.md), [wire contract](../robot/contract/README.md),
[simulation specification](../robot/simulation/SPEC.md), and
[acceptance run template](ACCEPTANCE_RUN_TEMPLATE.md).

## 1. What acceptance means

Annie passes the integrated simulation demo when a real rendered camera stream
drives measured model inference, a possible concern produces one audible
check-in, actual supported audio input drives the resident response branch,
and the family app shows the correct outcome and can send an audibly played
reply. Every consequential step has traceable evidence. Reassurance, help,
silence, uncertainty, and component failure must behave differently.

The motivating resident is Ellis's grandma. Acceptance uses synthetic scenes
and staged adult participants; it does not require her participation or an
actual person falling. A rigid person lying on the floor establishes a
posture-based check-in demo, not detection of the transition of falling.

Perfection here means explicit behavior, no hidden substitutions, reproducible
evidence, and no unexplained failures in the declared test envelope. Passing a
finite test set cannot establish universal safety, clinical accuracy, or
readiness for unsupervised eldercare.

### 1.1 Release gates and permitted claims

| Gate | Required proof | Permitted claim |
| --- | --- | --- |
| G0: deterministic software | Contract, policy, persistence, API/UI, failure handling tests with controlled inputs. | The software workflow behaves correctly on the declared test inputs. |
| G1: embodied simulation | Real MuJoCo joint actuation, measured pose, robot-mounted camera, authored-map missions, actual image inference; G0 still passes. | A simulated robot observes and navigates the tested scenes. State explicitly that locomotion uses the Go1 surrogate. |
| G2: integrated family demo | G1 plus verified prompt playback, actual supported audio interpretation, all core scenario scripts, family alert/acknowledgment/message playback, repeatability. | The complete assistance workflow works in the declared simulation and audio setup. |
| G2-N: external notification add-on | G2 plus a real configured test destination receives the intended notification and returns the available receipt. | That named notification channel was delivered in a controlled test. |
| G3: physical rehearsal | G2 plus physical Go2 Air capture, measured motion, stop validation, GX10 run, and named audio hardware tests in a controlled area. | The tested workflow ran on this named hardware setup. |
| G-DIMOS: full SDK integration | Actual configured DimOS stack produces synchronized streams and executes commands with SDK/module/version evidence. | Annie uses the demonstrated DimOS integration. Reusing a policy alone does not pass this gate. |

G-DIMOS and G2-N are independent claims. Direct MuJoCo can pass G1/G2 while
full DimOS remains incomplete. G2 may use the family web app as the alert
channel, but cannot then claim an SMS, phone call, or push notification.
Recorded clips may supplement a live demonstration; they never replace a
required live branch without an explicit reduced-scope label.

### 1.2 Priority, results, and release rules

- **P0:** a mandatory gate invariant or core behavior. Any failure blocks that gate.
- **P1:** quality required before its named feature is included in the demo.
  Removing a feature narrows the claim; it does not turn its failed test green.
- **P2:** stretch work; never trades against a P0 fix.
- Record PASS, FAIL, BLOCKED, or NOT RUN for every applicable test. SKIPPED and
  unavailable dependencies are not PASS. N/A requires a named excluded feature.
- Tests identify source mode, code revision, configuration, and evidence.
  A passing mock test cannot be counted as a passing model/hardware test.
- Unless a row explicitly says otherwise, all acceptance rows in a claimed
  gate are P0. No unresolved P0/P1 issue may remain in an included feature.

## 2. Frozen demo profile and measurement rules

Before a run, freeze the code commit and any working-tree patch hash, model
name/weights or hosted revision if available, prompt hash, scene seed, simulator
and asset revisions, runtime dependencies, API schema version, thresholds,
output/input devices, and exact enabled integrations. Save the profile with the
run evidence. A changed model, prompt, threshold, scene generator, microphone,
or policy invalidates affected results and requires the relevant rerun.

Values below are chosen engineering acceptance targets, not measured current
performance, clinical standards, or provider guarantees. Changing a value
requires a rationale and a new profile version before the evaluated run.

| Measurement | Acceptance target | Measurement boundary |
| --- | --- | --- |
| Policy confidence threshold | At least 0.80, preserving current demo setting. | Model score is not a calibrated probability. Unknown remains possible at any score. |
| Qualifying evidence | Two distinct captures, at least 250 ms and at most 5 s apart, in one map/run; no accepted contradictory observation between. | **Target change:** distinct UUIDs on the same capture do not count twice. Use sequence/capture provenance. |
| Frame age for policy | 0–5,000 ms at admission, inclusive; future timestamps rejected. | Timestamp is capture time, never inference completion time. |
| Frame/pose skew | At most 100 ms; target at most 50 ms on simulation. | Record both timestamps or prove capture is atomic. Missing skew evidence is unverified. |
| Source camera sampling | Target 2 captures/s, 640×480 baseline. | Physics/rendering rates are separate. Lower inference resolution must be in the profile. |
| Capture-to-admitted inference | p95 at most 3 s; no result older than 5 s used in policy. | At least 100 attempts in the latency qualification set; count rejected/time-out attempts separately. |
| Accepted inference cadence | p95 gap at most 2 s during the live observation segment. | Slow models must not create an unbounded frame backlog. |
| Second evidence → check-in creation | At most 1 s. | From admission of the second qualifying frame to committed event. |
| Question request → audible start | At most 2 s in the qualified output setup. | Requires observed playback; a generated WAV is insufficient. |
| Question request → playback end | At most 6 s for the fixed short prompt. | The configured phrase and speaking rate must meet this target. |
| Question completion watchdog | 8 s from command issuance. | No verified completion by this point produces communication-failure attention. |
| Resident response window | 8 s after verified question playback completes. | **Target change:** current policy starts this window when the command is queued. |
| STT processing grace | At most 2 s after the response cutoff, only for an already registered utterance captured within the window. | Does not admit speech captured after the cutoff or extend repeatedly. |
| Eligible help → escalation | At most 1 s after valid help intent reaches policy. | Bypasses the remaining response window. |
| Deadline → escalation decision | At most 1 s after response cutoff plus applicable processing grace. | Timer runs without new frames, requests, or open browser tabs. |
| Committed event → connected family UI | p95 at most 1 s, maximum 2 s. | Measure visible UI reconciliation, not only WS send completion. |
| Family command acceptance | At most 1 s to identified queued/accepted response. | Completion is separately measured and displayed. |
| Family text → audible start | At most 3 s for a bounded short message in the idle playback setup. | Queued behind higher-priority work must be visibly waiting. |
| Status freshness | Stale by 3 s without a new measured status. | Disconnected/unknown indication by 5 s, never a fabricated idle state. |
| UI resynchronization | At most 3 s after a healthy connection is re-established. | Includes events, acknowledgment, commands, map, and active incident. |
| Software-stop response in simulation | New movement targets inhibited within 250 ms; measured speed below 0.05 m/s within 1 s; additional displacement at most 0.20 m. | Test at the configured maximum demo speed. This is not hardware e-stop performance. |
| Waypoint completion | Within 0.30 m for at least 0.5 s with no prohibited contact; heading error at most 0.25 rad only when a heading goal is specified. | Measured pose and speed, not target position or planner state alone. |
| Mission duration | At most 150 simulated seconds; progress stall for 5 s fails the mission. | Report simulation and wall durations. A blocked mission fails rather than teleporting. |

Use nearest-rank p95 with the method and denominator recorded. Failed attempts
remain in failure-rate counts; do not present successful-only timing as total
system latency. Report cold start separately, then state the actual demo warmup.

Run G2 at 1× simulation speed. Capture timestamps are real wall capture times;
simulation step time is a separate field in the evidence record. Monotonic time
drives in-process deadlines. Pausing simulation pauses new evidence, not an
already-issued real-time check-in. Accelerated deterministic tests use one
injected virtual clock consistently and are labeled G0, not live G2 runs.

The eight-second reply window is a staged-demo setting, not a validated response
time for Ellis's grandma or other residents. Keep it configurable. Any eventual
resident-facing deployment needs a separately evaluated interaction pace,
language, hearing/accessibility setup, and escalation policy. G3 is still a
controlled rehearsal, not signoff for unsupervised use.

## 3. Invariants across all components

| ID | Required invariant and rejection test |
| --- | --- |
| INV-01 | Exactly one incident policy owner. Feeding the same stream through multiple transports cannot create duplicate incidents. |
| INV-02 | Preserve capture ID, capture time, map ID, camera/observer pose, source, and model provenance through inference and evidence. Reject altered identities at the receiving boundary. |
| INV-03 | Missing, unknown, stale, occluded, invalid, or failed inference is never converted to resident safety or a fabricated fall. |
| INV-04 | Frame/scene ground truth remains outside the model request. Strip scenario names, posture labels, revealing filenames, and captions that disclose expected answers. |
| INV-05 | Robot coordinates describe the observer. They are never presented as calibrated resident/object coordinates without an independently validated transform and measurement. |
| INV-06 | Queued, accepted, executing, synthesized, playback-reported, physically observed, failed, and uncertain have distinct meanings. Display only what the evidence supports. |
| INV-07 | Family acknowledgment never implies the resident is safe or that emergency services were contacted. |
| INV-08 | One episode produces at most one active concern and one logical escalation; transport retries retain the same delivery identity. |
| INV-09 | An unresolved concern survives service restart and app disconnection. Absence of observations does not silently clear it. |
| INV-10 | No autonomous motion or notifications from free-form model text, captions, transcripts, or advisory-agent suggestions. |
| INV-11 | No automatic switch from local to cloud, simulator to hardware, live to mock, or one paid provider to another. |
| INV-12 | Raw resident frames stay within the trusted local capture/compute boundary. Only explicitly configured synthetic media uses the authorized cloud route. |
| INV-13 | Live capability indicators reflect probes/receipts, not just configured flags. A process health response is insufficient for readiness. |
| INV-14 | Every irreversible external effect has a stable ID and a recorded outcome or explicit uncertainty. Do not blindly retry an unknown delivery. |
| INV-15 | A result is accepted only in the matching run/map context. Reset creates a new map/run identity and invalidates queued old-world motion. |

## 4. Incident state machine: exact target behavior

This is the sole policy, hosted in `robot/app_backend/` for this milestone. State
names are conceptual until added to the versioned contract. Existing event names
may be retained for compatibility, with typed reason/correlation fields added
through a migration. `fall_confirmed` continues to mean escalation, not diagnosis.

| State | Meaning | Allowed exits |
| --- | --- | --- |
| MONITORING | No active episode. Data availability is tracked separately. | One valid risky observation → CANDIDATE. |
| CANDIDATE | One eligible present-person, lying, floor/chair observation. | Eligible second capture → QUESTION_PENDING; contradictory accepted observation/gap/map change → MONITORING. |
| QUESTION_PENDING | Incident and question command committed together; one question requested. | Verified playback completion → AWAITING_REPLY; valid help → ATTENTION_REQUIRED; playback failure/watchdog → ATTENTION_REQUIRED with communication reason. |
| AWAITING_REPLY | Question actually played; input is monitored until deadline. | Qualified reassurance → REASSURED; qualified help → ATTENTION_REQUIRED; healthy-input timeout → ATTENTION_REQUIRED with no-response reason; input failure → ATTENTION_REQUIRED with communication reason. |
| REASSURED | Resident gave qualified reassurance for this episode. | Two affirmative recovery observations or deliberate logged operator reset → MONITORING. Continued risky frames alone do not flood check-ins. |
| ATTENTION_REQUIRED | Help, no response, or failed communication requires family attention. | Family acknowledgment records receipt only. Explicit resolution/recovery workflow is separate and logged. |

### 4.1 Observation qualification

A risky candidate is `person=true`, `posture=lying`, known `location=floor` or
`chair`, confidence at least 0.80, validated identity, correct map, and fresh
capture. The claim is a possible welfare concern. Lying on a chair may be
intentional; the question resolves uncertainty rather than declaring a fall.

The two captures must be genuinely distinct acquisitions and satisfy section 2.
Identical pixels can occur in a static scene; image similarity alone is not a
duplicate detector. Re-delivering the same capture under a new UUID is invalid
producer behavior. The adapter must preserve sequence/identity accurately.

An accepted bed/standing/sitting/unknown/low-confidence/empty observation breaks
the pre-incident candidate streak. Rejected stale/duplicate/wrong-map packets
do not advance the streak; elapsed time still expires it. An explicit camera or
inference discontinuity invalidates a pending candidate.

Once an episode exists, empty frames, a camera turn, occlusion, low confidence,
or a map reset do not establish recovery. **Target change:** re-arming requires
two fresh, same-resident-assumption, confidently present and affirmatively
non-risky observations at least 500 ms apart within 5 s, or a deliberate logged
operator reset. With one resident, the assumption is recorded; identity across
different people is not claimed. An active attention alert is never auto-erased
by recovery; record recovery as new evidence and retain its history.

### 4.2 Question and reply semantics

Use one short approved prompt, for example: “Are you okay? Please say okay or
help.” Use the resident's preferred name only if configured; no hardcoded family
relationship is required. A repeated delivery of its command ID must not replay
it. Stop/hold near the observation waypoint while checking in; do not approach
or touch the resident automatically.

The question's command ID, incident ID, playback target, and receipts are linked.
The normal response timer starts on verified playback completion, never queue
acceptance or synthesis. Prompt failure, muted/unavailable output, missing
completion, missing input, or unsupported audio generates an explicit
communication-failure reason. It cannot be described as the resident's silence.
Unknown audibility remains unknown even if a browser fires `ended`.

Help may interrupt question playback and escalates immediately. Reassurance
during playback is allowed only when the input path distinguishes the resident
from the speaker echo and binds the utterance to this incident. Otherwise it
remains ambiguous; the ordinary response window still opens after playback.

For the first release, keep intent interpretation deliberately bounded:

| Input | Target interpretation |
| --- | --- |
| “help”, “help me”, “I need help” | Help. |
| “okay”, “OK”, “I'm okay”, “I am okay”, corresponding “OK” forms | Reassurance only with trusted incident/audio correlation and validated recognition quality. |
| “not okay”, “I'm not okay”, “I can't get up”, “okay, help me” | Help/concern; never reassurance. Add explicit tested patterns rather than substring matching. |
| “maybe”, unrelated speech, unintelligible speech, unsupported language | Ambiguous; does not clear the episode. |
| Playback echo saying “okay or help” | Echo, not a resident response. |
| “No audio attached” or provider commentary | Inference failure, not a transcript of the resident. |
| No detected speech with a verified healthy input path | No response after the full window. |

Text alone does not establish speaker identity or recognition confidence. The
current audio service has no calibrated confidence field. Do not populate
`VoiceHeard.confidence=1` just to satisfy the current model. Add explicit reply
provenance/quality semantics, use a measured STT confidence policy if available,
or expose a clearly labeled operator-confirmed reply mode. Operator confirmation
can pass G0; it cannot pass autonomous audio interpretation in G2.

A reply must carry a stable utterance ID, capture start/end, incident ID, source,
and available quality evidence. Deduplicate by utterance ID. Speech from before
the question, another incident, or after the response cutoff cannot retroactively
clear an escalation. Allow the bounded two-second STT grace only for an utterance
registered before the deadline whose capture ended within the window. If STT
does not finish within grace, escalate with recognition-unavailable context.

### 4.3 Timers, persistence, and races

Persist the episode, evidence pair, question state, deadlines, command, and
outbox effects. Test crash points before/after each commit and external dispatch.
Recovery resumes the same logical episode; it does not ask the same question
again or discard the timer. If previous playback completion is uncertain,
surface that uncertainty and request family attention rather than guessing.

The boundary rule is explicit: captured speech ending at the response deadline
is eligible if its registration and processing satisfy the grace rule. Speech
ending later is not. Serialize deadline/reply transitions. A committed
escalation cannot be undone by late reassurance; record the later response as
an update for family. Repeated ticks, same-time replies, and restart recovery
must never create a second logical escalation.

### 4.4 Required contract evolution

Do not solve the new behavior by hiding extra meaning in free-text `detail`,
inventing model confidence, or keeping a second untyped dictionary protocol.
The following are minimum semantic additions; Ellis coordinates exact names and
versioning with Henry, Roger, and Sam before implementation.

| Domain object | Required target semantics | Migration concern |
| --- | --- | --- |
| Capture reference | Run ID, stable capture ID/sequence, camera ID, capture time, pose time/skew, map, source. | Current frame UUID/time/pose remains usable; additional provenance must be introduced explicitly rather than passed as forbidden extras. |
| Incident | Stable episode ID, state, typed reason, complete evidence references, question command, response deadline, recovery/resolution history. | Distinguish episode identity from event identity; map legacy `fall_suspected` event correlation deliberately. |
| Question receipt | Command/incident/player IDs, synthesized/start/end/failure phase, receipt timestamp, available device evidence. | Current command states are too coarse to make all audio claims; add typed phase/receipt data without redefining `accepted` as playback. |
| Utterance/reply | Stable utterance ID, capture start/end, incident ID, source, transcript status, available recognition/quality evidence, intent. | Existing required numeric `VoiceHeard.confidence` cannot represent a provider with no confidence; migrate that contract rather than fabricate it. |
| Command | Stable client idempotency key/ID, target/run/map where relevant, issued/expiry time, correlation, cancellation/supersession reason. | Retries after uncertain POST acceptance must reconcile; expired old-map motion must never execute after reset. |
| Delivery | Incident/delivery/provider IDs, accepted/delivered/failed/uncertain, attempt timestamps and bounded reason. | Transport acceptance is not a receipt on the family device. |
| Capability status | Per-capability ready/stale/disabled/failed/unverified, source, last verified time and bounded reason. | Keep `GET /health` liveness separate from readiness and synthetic/hardware labels. |

Maintain one canonical definition, generated schema, and compatibility fixtures.
For a breaking change, use a new explicit version or adapter; do not silently
change v0.1 meaning. Test old/new supported clients together. Keep legacy mock
fixtures visibly mock; a compatibility adapter cannot manufacture missing live
provenance. Update docs and remove obsolete paths once the migration completes.

## 5. Component acceptance: contracts, robot, navigation, and perception

### 5.1 Contract and transport tests (G0)

| ID | Test | Pass criterion |
| --- | --- | --- |
| CON-01 | Round-trip every public payload through producer schema, JSON, consumer schema. | No semantic loss; examples and generated schemas agree; unsupported versions fail explicitly. |
| CON-02 | Invalid types, numeric strings where forbidden, NaN/Inf, missing IDs, extra fields, oversized strings/media, malformed UUIDs. | Bounded 4xx response before side effects; rejected payload bodies/media are not echoed into logs. |
| CON-03 | Retransmit the same capture, command, receipt, acknowledgment, and notification request 20 times. | One logical record/effect; original correlation preserved. Different content under the same ID is rejected. |
| CON-04 | Reorder status, maps, observations, and receipts. | Older state cannot overwrite newer state; wrong-map poses rejected/quarantined; terminal command states do not regress. |
| CON-05 | Two events share one millisecond, then reconnect using the event cursor. | Both recovered exactly once in UI; cursor strategy has an explicit tie-break or overlap/dedup. |
| CON-06 | Fill a WS subscriber queue and disconnect it while events continue. | Bounded memory; explicit resync; complete REST recovery including acknowledgments. |
| CON-07 | Wrong/missing token, unapproved origin/host, forged forwarded headers. | Access follows the documented deployment profile; no accidental remote unauthenticated control. |
| CON-08 | Oversized streamed request without trustworthy Content-Length. | Request stopped at route-specific byte bound. JPEG and WAV bounds both have edge tests. |
| CON-09 | Command POST times out after server commit. | Retry/reconciliation finds the same stable command, not a second motion/speech action. Requires client idempotency semantics. |
| CON-10 | Full-frame bytes/URLs submitted to app event, memory, or advisory endpoint. | Rejected before persistence/egress; approved crop path remains narrowly scoped. |

### 5.2 Robot/simulator adapter tests (G1)

| ID | Test | Pass criterion |
| --- | --- | --- |
| ROB-01 | Cold start with valid and missing assets. | Versioned scene/model loads or actionable failure; no physical endpoint fallback or hidden downloads during the demo. |
| ROB-02 | 10 resets, including one during a mission/check-in. | New map/run ID each time; old motion cancelled; old evidence preserved under its original map; no coordinate mixing. |
| ROB-03 | Capture 100 camera/pose pairs while moving and turning. | IDs/timestamps unique per acquisition, skew within target, camera is robot-mounted and pose corresponds to capture time. |
| ROB-04 | Move the spectator camera without moving the robot. | Perception camera does not change; spectator camera cannot silently become the model input. |
| ROB-05 | Stop render/capture/status individually. | Correct capability degrades within targets; model/policy receives no fabricated replacement observations. |
| ROB-06 | Verify physics integrity throughout a mission. | Finite state, no numerical warnings, no base-pose teleportation, no prohibited contacts; actual joint control drives displacement. |
| ROB-07 | Toggle simulation pause/speed. | State and time domains remain honest; G2 run uses 1×; active real-time incident still resolves. |
| ROB-08 | Terminate the bridge and restart it. | No duplicate execution, no replay of expired motion, one clear source of adapter status; recovery uses stable IDs. |

### 5.3 Motion and command tests (G1)

| ID | Test | Pass criterion |
| --- | --- | --- |
| NAV-01 | Goto each of three waypoints, then patrol all three in order. | Measured arrival meets section 2; receipt has actual completion evidence; no forbidden collision. |
| NAV-02 | Forward, turn, stop at maximum configured demo velocity. | All finite; stable posture; measured software-stop targets met. Test each direction the UI exposes. |
| NAV-03 | Stop/resume a mission, stop while inference is hung, stop during speech. | Stop is independent of inference/audio; no new motion until explicit resume; original mission state is well defined. |
| NAV-04 | Unreachable waypoint and blocked path. | Bounded failure with reason; no teleport, spin forever, or false completion. Clear obstacle and issue a new command to recover. |
| NAV-05 | Queue duplicate, expired, conflicting, and out-of-order commands. | Stable IDs deduplicate; stop preempts motion; queue limits enforced; superseded commands have terminal reasons. |
| NAV-06 | Place static furniture and resident exclusion geometry near the route. | Whole robot footprint avoids them; resident exclusion margin at least 0.5 m in simulation; report minimum clearance. |
| NAV-07 | Introduce a new obstacle after planning. | Detect/replan or fail stopped before contact. If only authored static maps are supported, explicitly exclude dynamic obstacle avoidance from the claim and demonstration. |
| NAV-08 | App says completed while command still moving (fault injection). | Reconciliation/test detects contradiction; implementation cannot derive completion from HTTP acceptance alone. |

The 0.5 m simulation margin is a test configuration, not a certified safe human
distance. Physical clearances need separate validation. Never use the person's
authored position as an unacknowledged perception result.

### 5.4 Vision tests (G1/G2)

| ID | Test | Pass criterion |
| --- | --- | --- |
| VIS-01 | Submit actual rendered JPEG plus capture metadata. | Trace proves image bytes reach the selected model; resulting observation preserves metadata; no scene labels supplied. |
| VIS-02 | Same text/configuration with different bed/floor/empty images; include a blank image control. | Outputs respond to pixels; an image-blind/static response fails modality qualification. |
| VIS-03 | `person=false` with standing/lying posture, or contradictory/invalid location output. | Reject or normalize to explicitly unknown semantics using a documented rule; never feed contradictory risk evidence into policy. |
| VIS-04 | Crop/occlude resident, turn away, change lighting/viewpoint. | Ambiguity remains unknown when evidence is insufficient; no invented measurements or safety inference. |
| VIS-05 | Provider timeout, 429, malformed JSON, unsupported media, truncated output, unavailable model. | Typed failure, bounded time, visible degraded state; no synthetic observation or automatic provider switch. |
| VIS-06 | Return a plausible observation after 5 s or from a previous map. | Excluded from current incident decisions; diagnostic display clearly marks stale/wrong-context result. |
| VIS-07 | Saturate capture while model runs slowly. | At most one active inference plus bounded latest pending frame; expired frames dropped, not accumulated. |
| VIS-08 | Replace capture time with completion time or tamper with pose/ID in provider response. | Boundary validation detects it; test fails if it rejuvenates evidence. |
| VIS-09 | Hardware-labeled or untrusted-source media to cloud endpoint. | Rejected before egress. Synthetic label alone is not sufficient unless produced by the trusted simulator path. |
| VIS-10 | Measure threshold behavior at 0.799, 0.800, 0.801 and malformed/out-of-range confidence. | Exact policy threshold respected; no claim that score equals probability. |
| VIS-11 | Run the locked evaluation set in section 9. | Meets category, end-to-end, latency, and uncertainty criteria with all attempts reported. |

## 6. Component acceptance: audio, family experience, memory, notifications

### 6.1 Speech output and input (G2)

| ID | Test | Pass criterion |
| --- | --- | --- |
| AUD-01 | Synthesize the fixed question and short family message. | Valid bounded audio, correct words, no command/text injection; synthesis receipt alone is labeled synthesized. |
| AUD-02 | Play through the selected laptop/browser or external speaker. | Human witness hears intelligible correct words at the staged resident position; player reports start/end; evidence names the device and volume. |
| AUD-03 | Mute output, block browser autoplay, unplug device, fail decoding, close player mid-clip. | No unqualified audible-success claim; timeout/failure visible; incident uses communication-failure reason. |
| AUD-04 | Replay the same speech ID and open two browser tabs. | Exactly one selected output plays it; no double-speaking; receipt bound to active player/session. |
| AUD-05 | Echo the question into the microphone. | Echo is excluded or classified ambiguous; “okay or help” from Annie cannot clear or falsely escalate the incident. |
| AUD-06 | Submit real supported audio for okay/help/negated okay/ambiguous/no speech. | Correct semantic outcome on section 9's audio corpus; unsupported modality/provider prose rejected. |
| AUD-07 | Disconnect/mute microphone, deny permission, stop stream, or stall STT. | Input-unavailable is distinguishable from resident silence; UI and incident reason agree. |
| AUD-08 | Old, duplicate, unrelated, late, and wrong-incident utterances. | No unauthorized clearing; one effect per utterance; deadline/grace behavior matches section 4. |
| AUD-09 | No numeric recognition confidence from provider. | No fabricated number; explicit quality/provenance handling; unverified text cannot close the incident. |
| AUD-10 | Audio body-size boundaries and metadata removal. | Legitimate maximum request reaches validation; oversized/truncated WAV fails before provider call; sensitive metadata absent from egress. |
| AUD-11 | Family speech queued during active question, then help arrives. | Priority is deterministic; no overlapping voices; cancellation/interruption state reported accurately. |
| AUD-12 | A browser reports `ended` while output is muted. | Stored meaning is player-reported completion, not proof of audibility; live audibility qualification fails. |

Live human microphone input is a separate capability from synthetic WAV upload.
Current synthetic-only cloud authorization does not authorize sending a real
participant's microphone recording to that route. Use a local STT path or
separately authorized real-audio route. A synthetic prerecorded voice may pass
G2 only when the demo explicitly claims a prerecorded synthetic resident reply;
it cannot be presented as live conversation or a verified DJI microphone.

### 6.2 Family application (G0, rechecked in G2)

| ID | Test | Pass criterion |
| --- | --- | --- |
| APP-01 | Empty startup, offline backend, no map/observations. | Explicit unavailable/empty state; no invented resident activity, battery, or location. |
| APP-02 | Receive concern and escalation. | Visible severity, reason, timestamp/age, evidence reference, source mode, and connection state; no diagnostic wording. |
| APP-03 | Acknowledge twice, refresh, reconnect, use another tab. | One durable acknowledgment; actor/time retained; other view converges; acknowledgment does not resolve resident safety. |
| APP-04 | Send a family message through queued → executing → completed/failed. | Stable command ID and honest progress; failure actionable; success only reflects available playback evidence. |
| APP-05 | Disconnect during check-in and reconnect after escalation. | Complete current state recovered within target without duplicate alerts or lost acknowledgments. |
| APP-06 | Two events have identical timestamps; WS overflows. | Both retained; refetch/dedup works; no missed escalation. |
| APP-07 | Change map/run while historical evidence is open. | Historical observation remains labeled with old map/time; never pinned to unrelated new coordinates. |
| APP-08 | Narrow 320/375 px screen, 200% zoom, keyboard-only controls. | Core alert/ack/message workflow usable without clipped controls; visible focus; labeled fields; status not color-only. |
| APP-09 | Screen-reader alert and repeated telemetry updates. | New incident is announced once; routine telemetry does not spam announcements or steal focus. |
| APP-10 | Caption/transcript containing HTML, scripts, misleading commands, or fake error instructions. | Displayed as inert text; no execution or control action. |
| APP-11 | Alternate Swift/UI profile loses its backend. | Explicit disconnected/mock mode; no silent hardcoded-live fallback. |

Use at least 44×44 CSS px targets for the primary mobile actions. This is the
project's usability target, not a claim of formal accessibility certification.

### 6.3 Evidence and memory (G0/G2)

| ID | Test | Pass criterion |
| --- | --- | --- |
| MEM-01 | Ask about an observed synthetic object. | Answer cites frame ID, capture time, observer pose/map and source; every substantive claim supported by retrieved evidence. |
| MEM-02 | Ask about an unobserved object or interval. | Explicitly unanswerable; no plausible invented location. |
| MEM-03 | Move object after the last observation. | Wording says last observed, with time; never claims current position without new evidence. |
| MEM-04 | Conflicting observations over time. | Preserve time ordering/context; show uncertainty/conflict rather than silently merging unrelated records. |
| MEM-05 | Evidence expires or memory reaches its cap while an incident remains. | Incident retains its decision evidence/metadata; unavailable crops are marked unavailable, not broken invented links. |
| MEM-06 | Restart database/API and reconnect UI. | Evidence and acknowledgments persist; timestamps and map identity unchanged. |
| MEM-07 | Request arbitrary file path or unapproved crop. | Rejected/404; no access outside the approved crop store. |

Captions plus `(x, y, yaw, time)` qualify as observation memory. Persistent
entity identity, calibrated 3D object positions, temporal relations, provenance,
uncertainty, and query evaluation are additional requirements for a claimed 4D
spatiotemporal knowledge graph. That claim is excluded from this core release.

### 6.4 Notification delivery (G2-N only)

| ID | Test | Pass criterion |
| --- | --- | --- |
| NOT-01 | Help and timeout to the designated test recipient. | Exactly one logical alert per episode; correct reason/time/reference; actual device receipt observed. |
| NOT-02 | Provider accepts then times out or returns no delivery evidence. | State is accepted/uncertain, not delivered; reconcile by stable provider/idempotency ID. |
| NOT-03 | Provider returns rejection, quota error, or delayed delivery. | Failure/delay visible in family app; local incident preserved; no silent channel substitution. |
| NOT-04 | Retry after restart. | No duplicate send when provider already accepted; unresolved uncertainty remains explicit. |
| NOT-05 | Verify outbound payload. | Only approved bounded alert data; no full frames, secrets, or unrestricted transcript dump. |

A test notification must go to an explicitly authorized test destination.
Specifying this test does not itself authorize contacting anyone. If the
provider lacks delivery receipts, name exactly what device observation proves.

## 7. Deterministic policy acceptance matrix (G0)

Use an injected clock and validated synthetic observations. No wall-clock sleeps,
model calls, or robot connection are necessary. Count events by incident/episode,
not merely total rows. A suspicion plus a timeout annotation plus one escalation
is three different records, not three escalations.

| ID | Setup/action | Exact expected result |
| --- | --- | --- |
| POL-01 | Two high-score bed-lying captures, then ten more. | Zero check-ins, zero escalations. |
| POL-02 | One risky floor capture, then normal posture. | Candidate cleared; zero check-ins. |
| POL-03 | Two qualifying floor captures 500 ms apart. | One episode, one suspicion, one identified question; both evidence IDs retained. |
| POL-04 | Same as POL-03 with lying/chair. | One check-in for a possible concern, not a declared medical fall. |
| POL-05 | Same capture delivered twice, including transport retries. | Counts once; no question from replay. |
| POL-06 | Distinct IDs falsely attached to one capture, or captures 249 ms apart. | Does not satisfy the evidence pair; producer-provenance failure visible. |
| POL-07 | Pair at 250 ms and 5,000 ms boundaries; then at 5,001 ms. | First two permitted if otherwise valid; last starts a new candidate, never completes the expired pair. |
| POL-08 | Confidence 0.799/0.800/0.801; unknown posture/location; absent person. | Only qualifying known risky inputs at or above threshold contribute. |
| POL-09 | Risky → unknown/low-score/empty → risky. | No pair across the accepted interruption. |
| POL-10 | Stale by 5,001 ms; age exactly 5,000 ms; future by 1 ms. | Only exact 5,000 ms case is freshness-eligible; none bypasses other rules. |
| POL-11 | Reordered timestamp, wrong map, previous-run result. | No new evidence or incident from invalid context; rejection reason recorded. |
| POL-12 | Candidate in map A then candidate in map B. | No cross-map pairing; pending old episode, if any, remains historically intact. |
| POL-13 | Question queued but not played. | Eight-second resident-response window has not started; playback watchdog still runs. |
| POL-14 | Verified playback completes at time T. | Deadline is exactly T + 8 s; no no-response escalation at T + 7.999 s. |
| POL-15 | Healthy input, no utterance; tick at deadline and repeatedly after. | One no-response outcome and one logical escalation; no extra question/escalation. |
| POL-16 | Correlated qualified reassurance inside window. | One reassurance outcome; no escalation for that episode. |
| POL-17 | Correlated help during question or response window. | Immediate single escalation; remaining response timer cannot create another. |
| POL-18 | Negated okay, “okay, help me”, ambiguous speech. | Never reassurance; concern phrases escalate, ambiguous speech leaves deadline active. |
| POL-19 | Echo, unrelated, old, wrong-incident, or low-quality reassurance. | Does not close check-in; no invented confidence. |
| POL-20 | Utterance ends exactly at deadline; STT resolves within 2 s grace. | Eligible if registered in-window; timer handles it deterministically. |
| POL-21 | Utterance ends 1 ms after cutoff or STT arrives after grace. | Cannot clear/rewind escalation; later update is recorded with actual timing. |
| POL-22 | Speaker/microphone/STT fails before or during response window. | Communication-failure attention; never mislabeled resident silence or safety. |
| POL-23 | Continue risky frames for 60 s after reassurance/escalation. | No repeated episode from the same continuing condition. |
| POL-24 | Empty/occluded/unknown frame after episode, then risky frame. | Does not re-arm; losing sight of resident is not recovery. |
| POL-25 | Two affirmative recovery captures, then new qualifying risky pair. | New episode allowed with new IDs; prior alert/evidence/acknowledgment preserved. |
| POL-26 | Family acknowledges while resident has not replied. | Receipt recorded; resident timer and concern remain independent. |
| POL-27 | Restart during question, response window, after escalation, and after acknowledgment. | Same durable state/effects restored; elapsed deadline resolved once; no replayed speech or lost alert. |
| POL-28 | Help and deadline race; duplicate STT callbacks; two application requests at once. | Serialized result, no conflicting terminal outcomes, no double escalation. |
| POL-29 | Wall clock jumps forward/backward while process runs. | Monotonic deadline remains correct; timestamp anomaly recorded without rejuvenating frames. |
| POL-30 | No frames, no API traffic, no connected UI after question. | Background deadline still resolves; tests do not rely on a subsequent request to trigger it. |
| POL-31 | Restart when playback receipt/delivery status is uncertain. | Preserve uncertainty, reconcile by IDs, and surface communication failure rather than asserting completion. |
| POL-32 | Advisory provider returns an instruction to cancel the incident or move the robot. | No policy/motion effect; advisory output remains bounded inert data. |

## 8. End-to-end scripts (G2)

Every script records a synchronized screen capture or equivalent timestamped
UI evidence, simulator telemetry, observation/incident/command IDs, actual audio
mode, and event history. Camera frames go through the selected model. Do not
press a synthetic “fall/help/okay/timeout” button during these scripts.
Those buttons remain useful G0 diagnostics and must be visibly labeled.

Before each script: select the frozen profile and scene; verify fresh pose/frame,
model readiness, input/output readiness, and family connection; record run ID;
clear only through a documented test reset. Do not erase failed attempts.

### E2E-01: quiet home / resident resting on bed

Purpose: demonstrate that ordinary rest does not create concern.

1. Start the robot at its configured home and patrol to the observation point.
2. Keep the resident clearly visible resting on the bed for 30 s.
3. Collect at least ten actual model observations and inspect their provenance.
4. Open the family app and inspect history, source labels, map, and last-seen age.

Pass: no question, no concern/escalation, no external notification; bed is correctly
recognized in the qualified clear views. Unknown-only outputs may avoid false
alerts but fail vision usefulness/accuracy qualification. A normal state means
only no detected concern in the observed interval, not proof the resident is safe.

### E2E-02: sustained floor posture and silence

Purpose: the principal assistance demonstration.

1. Start with a non-concerning scene, then switch to a staged floor-lying posture
   or use the documented controllable resident placement. Record whether the
   transition is animated, teleported, or a scene switch; claim only what ran.
2. Robot reaches an observation point without touching/approaching the resident.
3. Model produces two eligible risky captures; record capture/admission times.
4. One concern appears; robot holds position; the question audibly plays once.
5. Provide no reply while the verified input path remains healthy.
6. Wait for the real eight-second window; no synthetic timeout control.
7. Family receives one attention alert with reason, time, evidence, and source.
8. Family acknowledges; status survives refresh and a second tab.
9. Family sends “I'm coming to check on you”; correct message audibly plays.

Pass: all transitions meet section 2; no duplicate question/escalation; family
message receipt reflects playback. A concern is visible within 10 s of the first
qualifying clear view in the qualified scenario. With the maximum playback and
processing allowances, escalation is visible within 22 s of admission of the
second risky observation (1 + 8 + 8 + 2 + 1 + 2 s). Actual stage timings are
reported individually; do not conceal a missed sub-target behind the total.

### E2E-03: resident reassures Annie

Follow E2E-02 through audible question. Provide the qualified “I'm okay” audio
through the declared input path. Verify transcript, quality evidence, incident
correlation, and capture timing. Continue risky posture observations for 30 s.

Pass: one reassurance outcome, no escalation for that episode, no repeated
questions from continuing posture. Family sees that the resident responded,
without language implying a medical assessment. A later new episode requires
the documented recovery/reset sequence.

### E2E-04: explicit help, including negation

Run once with “help me” and once with “I'm not okay.” Include an interrupting
reply while the question is playing when the qualified audio path supports it.

Pass: immediate attention after accepted help intent, no wait for timeout, no
reassurance substring bug, one escalation, preserved transcript provenance and
resident-response timing. Echo handling must still pass.

### E2E-05: uncertainty, partial view, and looking away

Observe an occluded resident, then turn away; deliberately produce unknown and
empty frames. Repeat once before any incident and once after a concern exists.

Pass: uncertainty displayed; no new incident from unavailable evidence; existing
concern does not disappear or re-arm merely because the robot stopped seeing
the resident. Restore the view and demonstrate continued correct processing.

### E2E-06: slow model and disconnected input/output

Delay image inference beyond five seconds, then restore it. Separately interrupt
the output player before the question ends and the input path during the window.

Pass: stale image result never triggers a fresh incident; model failure cannot
freeze timers/stop controls. Audio failure is reported as communication failure,
not resident silence. The family retains a clear actionable unresolved concern.

### E2E-07: family offline and application restart

Disconnect the family app before escalation; restart the app API during a pending
check-in using the documented persistence directory; reconnect after its deadline.
Repeat with a command accepted before the bridge loses its connection.

Pass: same episode and stable command IDs recover; alert/acknowledgment survives;
no lost timeout, no replayed motion/speech, no silent mock fallback. If any external
effect is uncertain, it remains explicitly uncertain until reconciled.

### E2E-08: cited memory and absence of evidence

Observe a named synthetic object, ask where it was last seen, then ask about an
unobserved item. Move the first object without a subsequent robot observation.

Pass: supported answer cites the actual frame/time/map and observer position;
unobserved item is unanswerable; moved item's answer remains “last observed,”
not a claim to current location. Run after a map reset to expose coordinate mixing.

### E2E-09: repeated command / stop during busy inference

Send goto, duplicate its request/receipt, stall the brain, then issue software
stop. Resume deliberately and finish the route.

Pass: one motion mission, bounded stop independent of model latency, measured
arrival, correct receipts, no accidental execution on a physical endpoint.

### E2E-10: declared demo recovery

From a clean stopped setup, the designated operator follows only the checked-in
runbook. Exercise missing model, missing asset, occupied port, and unavailable
optional provider one at a time. Recover using the documented steps.

Pass: no undocumented source edits, hidden terminal injections, or model/route
switches. Failure is visible and recoverable; the chosen reduced-scope demo, if
needed, is honestly labeled. Full claims require the full qualifying profile.

## 9. Evaluation set, quality targets, and repetition

### 9.1 Rendered-image evaluation corpus

Use separate development scenes and a frozen evaluation manifest. Minimum
evaluation set: 60 scene episodes, each at least 20 s and at least ten distinct
submitted capture attempts. Keep all scene/run seeds and frame hashes. Scene
labels are available to the evaluator only, never the inference prompt.

| Category | Episodes | Expected episode behavior |
| --- | --- | --- |
| Clearly visible floor-lying resident | 10 | Check-in from qualifying observed evidence. |
| Clearly visible chair-lying resident | 10 | Check-in for possible concern, with cautious wording. |
| Clearly visible bed-resting resident | 10 | No check-in/escalation; correct bed interpretation. |
| Normal standing/sitting | 10, five of each | No check-in/escalation. |
| Empty rooms | 10 | No invented person or concern. |
| Deliberately ambiguous/occluded views | 10 | Explicit uncertainty where annotated unresolvable; no confident invented safety or concern. |

Vary room arrangement, lighting, camera angle, distance, clothing/appearance
where assets permit, and partial occlusion. Record asset diversity limitations;
60 views of one rigid scan are not 60 different people. At least half the scene
seeds must not have been used for prompt/threshold tuning. Human-review labels
and whether the robot's actual camera view makes them visually answerable before
running model evaluation. Do not score hidden ground truth as visible evidence.

Acceptance thresholds:

- All 30 clear negative episodes (bed, normal, empty) produce zero false check-ins
  and zero false escalations in this set. Report `0/30`, not “zero false positives”
  without a denominator or an implied general population rate.
- At least 18/20 clear concern episodes produce a check-in within 10 s of the
  first qualified clear view, with at least 9/10 in each concern category.
- At least 95% correct present/posture/support interpretation across adjudicated
  answerable frames, and at least 90% per clear category. Missing/timeout/unknown
  on an answerable frame counts against end-to-end usefulness, not as correct.
- All deliberately unanswerable evaluated views avoid confident invented risk
  or safety. Report uncertainty rate separately; it is not ordinary accuracy.
- Meet section 2's latency/cadence targets; at least 95/100 inference attempts
  in the latency qualification are usable before the freshness cutoff.
- Report the full confusion matrix, missed concern episodes, false check-ins,
  false escalations, unknown rate, malformed outputs, stale results, and timeouts.
  Include timestamps and IDs for every error. Do not discard cold failures.
- Chosen demonstration scenes must pass 5/5 repeated attempts per required
  positive/reassurance/help/silence branch. A good average cannot excuse a flaky
  chosen live scene.

If the selected model cannot meet these criteria, G1 perception/G2 remains
blocked. A scripted ground-truth demo can still be shown as G0; relabel it.
Record practical limitations rather than quietly lowering the threshold after
seeing failed evaluation results. New tuning requires a fresh held-out subset.

### 9.2 Audio qualification corpus

Use at least 60 short clips from at least two permitted synthetic voices or
consenting staged speakers on an authorized local/approved route:

- 10 explicit help phrases.
- 10 clear reassurance phrases.
- 10 negated reassurance/concern phrases.
- 10 ambiguous/unrelated utterances.
- 10 silence/noise/non-speech clips.
- 10 playback-echo or wrong-turn/wrong-incident cases, including metadata tests.

Vary timing, speaking level, modest room noise, and pronunciation. Record actual
capture/output devices, STT version, transcript, semantic classification, and
latency. Score word transcription separately from incident intent.

Pass: at least 9/10 clear help and 9/10 clear reassurance handled correctly;
at least 9/10 explicit negated concerns reach help/attention; **zero false
reassurance** across every concern, ambiguous, noise, echo, and wrong-context
case. Missing/unsupported media is a failure, not an empty successful transcript.
Every selected live demo phrase passes 5/5 through the actual configured path.

The API's successful status code is never the scoring oracle. Compare against
the audible words and intended response. Provider text explaining that it did
not receive audio fails the test even when perfectly valid JSON surrounds it.

### 9.3 Repeatability and soak qualification

- Execute all E2E scripts once with evidence, then repeat E2E-01 through E2E-04
  ten consecutive complete cycles on the frozen profile. Zero P0 failures.
- Run a 30-minute soak with periodic patrol, observation, check-in, acknowledgment,
  message, and reconnect operations. No crash, deadlock, duplicate logical alert,
  growing stale-frame backlog, or missed deadline beyond the stated targets.
- Record process memory every minute after warmup, queue sizes, tasks, active
  connections, and pending effects. Investigate sustained growth; it must remain
  below declared caps and show a plateau. Establish numeric memory/disk caps for
  the selected machine before the run, not after observing its peak.
- Test ten scene resets and five service restarts across different policy states.
- Perform one clean-checkout install/start/run on the designated demo machine.
  Dependency/model downloads can happen before the demo and are timed separately.

These are finite qualification runs. Their report states sample count and
conditions and never claims 100% reliability outside the tested envelope.

## 10. Fault injection and recovery (G0, selected live checks in G2)

| ID | Injected fault | Required behavior/evidence |
| --- | --- | --- |
| FLT-01 | Brain unavailable, slow, overloaded, or returning malformed observations. | Bounded failure; camera/robot/API remain responsive; no manufactured perception; candidate continuity handled explicitly. |
| FLT-02 | Camera freezes but status continues. | Frozen capture identity/age detected; repeated JPEG delivery cannot count as new acquisitions; camera readiness degrades. |
| FLT-03 | Status freezes while camera continues. | Pose freshness/skew invalidates unusable evidence; map display shows stale location. |
| FLT-04 | Bridge or app dies immediately after command accepted. | Durable ID/outbox reconciliation; no duplicate action and no false failure/success on timeout alone. |
| FLT-05 | Database write failure, full disk, locked database. | No claim of durable alert/acknowledgment without commit; explicit degraded state; no continued unsafe accumulation. |
| FLT-06 | Crash at every incident/effect transaction boundary. | Recover same episode and effect identities; no half-written event, lost deadline, or duplicated message. |
| FLT-07 | Provider budget exhausted/corrupted ledger. | Fail closed before egress; preserve policy locally; no retries/fallback or quota reset. |
| FLT-08 | Audio player closes, browser mutes, input stream stops. | Accurate communication-failure context; question/response timer semantics preserved. |
| FLT-09 | WS backlog, network drop, duplicate or reordered messages. | Bounded queues, explicit resync, eventual current UI consistent with server. |
| FLT-10 | Invalid MuJoCo state, policy exception, blocked robot, missing asset. | Motion stops/physics pauses as appropriate; latched fault; deliberate reset required; no fake movement completion. |
| FLT-11 | Clock changes and stale async completion after reset. | Monotonic deadlines and run/map correlation prevent invalid transitions. |
| FLT-12 | Model sees text instructing it to say “safe,” send a message, or reveal secrets. | Output remains untrusted perception data; validation/action authority unchanged. |

A failed component must identify which capabilities remain available. “Degraded”
without a reason is insufficient for the operator to decide what the demo can do.

## 11. Privacy, data lifetime, and external-action boundaries

These are implementation acceptance checks for the existing local-first design,
not a separate compliance certification exercise.

- Test with synthetic media by default. No real resident data is required.
- Inspect actual outbound request bodies and destination allowlists using mock
  transports for automated checks. A comment saying “local” is insufficient.
- Full frames do not enter app REST/WS/events/memory logs or advisory prompts.
  Released crops have explicit provenance and policy; crops are still sensitive.
- Real resident/hardware source is blocked from the synthetic cloud profile.
  Source flags are trusted only from the correct producer; do not trust arbitrary
  clients to relabel real media as simulation.
- Secrets remain out of source, URLs, screenshots, receipts, and logs. Provider
  failures return bounded sanitized errors, not raw request/response dumps.
- Record a synthetic-run retention period before qualification: default seven
  days for local raw acceptance captures, with an explicit keep/delete manifest
  for selected reproducibility evidence. This is a target configuration; this
  documentation change does not delete existing files or authorize cloud uploads.
- Keep incident metadata needed for the run's audit until signoff; frame-cache
  eviction must not orphan decision evidence silently. No real-resident retention
  policy is implied by these synthetic defaults.
- Cleanup command, database path, media path, and budget ledger path are distinct.
  A demo reset must not erase the cloud-spend ledger or evidence from failed runs.
- Live provider tests are manual/opt-in and stay inside already authorized scope,
  media class, model route, destination, and remaining budget. The specification
  grants no new budget or authorization to contact third parties.

## 12. Physical Go2 Air and GX10 gate (G3)

Passing simulation does not automatically enable motion on hardware. Henry owns
the controlled robot rehearsal, Roger owns compute/audio, Ellis owns persistent
application behavior, and Sam owns the family display.

| ID | Hardware check | Required proof |
| --- | --- | --- |
| HW-01 | Identify the exact unit. | Go2 Air model/firmware, connection method, SDK revision, GX10 OS/runtime, actual ports and audio devices recorded. |
| HW-02 | Read-only capture/status first. | Live camera and measured status/pose on the actual unit; disconnect clearly detected; no simulation fallback. |
| HW-03 | GX10 image inference. | Actual frames processed locally on GX10 with measured end-to-end latency, memory use, model/weights, and source identity; laptop timings cannot substitute. |
| HW-04 | Physical stop and recovery. | Operator identifies the actual hardware stop method and verifies it under controlled conditions before autonomous mission rehearsal. Software stop is separately measured. |
| HW-05 | Bounded motion. | One short path in a cleared controlled area, deliberately low configured speed, nearby operator, verified stop; no people used as collision targets. |
| HW-06 | Physical map/pose alignment. | Waypoint position and camera/pose synchronization checked against the actual area; no reuse of simulated coordinates. |
| HW-07 | Speaker and microphone. | Identify installed/external speaker and mic; verify actual audible output, input capture, echo handling, and DJI integration if claimed. Air onboard audio remains unverified until tested. |
| HW-08 | Controlled staged concern. | Use a mannequin or deliberate safe lying posture by a consenting adult; no induced fall. Complete the same check-in/app workflow with hardware evidence. |
| HW-09 | Failure handling. | Network/compute/audio loss yields accurate degraded state and bounded motion behavior; active incident survives. |
| HW-10 | Repetition. | Five successful controlled runs of the chosen hardware demo, no unexplained motion/audio/incident failures; record physical limits and operator interventions. |

No on-robot person approach, touching, stairs, lifting, medication dispensing,
or autonomous emergency-service call is part of this gate. Simulation locomotion
policy must not be deployed to the Air merely because its simulated gait passed.

## 13. Stretch features: separate acceptance, never implicit completion

### Medication reminders (P2)

If included: a configured schedule with timezone triggers one identified prompt;
acknowledgment is explicitly self-reported; duplicates/restart/DST/time changes
do not repeat or lose the reminder; missed/declined/unknown are distinct; family
sees the reported status. “Taken” cannot be inferred merely from seeing a bottle,
hand motion, or hearing unrelated speech. No dose recommendation or dispensing.
Required scenarios: due once, early response, late response, no response, duplicate,
restart across due time, timezone change, and family acknowledgment independence.

### Temporal/4D memory (P2)

Before using that claim, define stable entity identities, relationship schemas,
valid-time versus observation-time, 3D coordinate frames/calibration, uncertainty,
provenance, contradictory evidence handling, expiry, and evaluated queries across
time. Test object movement, identity ambiguity, occlusion, map reset, stale edges,
and “unknown now.” Current caption retrieval does not pass this gate by itself.

### Advisory agents (P2)

Disabled unless explicitly configured. Only approved bounded text evidence;
citations checked against supplied evidence; timeout/failure cannot delay policy;
no motion, alert, notification, or incident-cancellation tools. Verify malformed
answers, fabricated citations, provider error, and exhausted budget with mocks.

## 14. Codebase and CI acceptance

The [architecture contract](ARCHITECTURE.md) is part of acceptance. A feature is
not complete if it works only through duplicated policy or hidden global state.

| ID | Codebase gate | Evidence |
| --- | --- | --- |
| ENG-01 | One canonical schema source and one incident engine. | Import/dependency review; generated schema check; no duplicated live/mock production policy. |
| ENG-02 | Policy independent of transport, model, database implementation, and global clock. | Deterministic tests can instantiate it without those services or wall sleeps. |
| ENG-03 | Thin routes and bounded adapters. | Review HTTP handlers, viewer, bridge and UI; each delegates to its responsibility; no all-purpose growing handler. |
| ENG-04 | Transactional state/effects and idempotent execution. | Crash/retry tests and persisted journal/outbox inspection. |
| ENG-05 | Explicit configuration/dependency injection. | One validated startup profile; no repeated magic thresholds, import-time network calls, or environment-based hidden behavior. |
| ENG-06 | Reproducible installation and clean boot. | Exact dependency/model/asset provenance, fresh checkout run, documented entrypoints, no reliance on another owner's untracked file. |
| ENG-07 | Automated tests reflect behavior and failure paths. | Policy/contract/adapter/restart tests; assertions on effects and identities, not only status code/implementation helper calls. |
| ENG-08 | CI includes all changed components. | Backend, brain/audio mocked suites, bridge/navigation unit tests, schemas, robot/frontend/viewer syntax checks; opt-in physics/live tests reported separately. |
| ENG-09 | No secrets, raw private data, unbounded resources, broad swallowed errors, or silent fallback. | Targeted diff review and boundary tests. |
| ENG-10 | One integrated family application contract. | Alternate UI consumes same API or is explicitly mock-only; no duplicate divergent incident backend. |
| ENG-11 | Documentation and readiness labels match behavior. | No “queued only” claim when playback is wired, no “voice connected” claim from configuration alone, no obsolete alternate run instruction. |
| ENG-12 | Reviewable integration and ownership. | Acceptance IDs in handoff, affected checks run, unrelated work preserved, final integration diff inspected. |

Current documented commands, to be run in a prepared test environment:

```sh
.venv/bin/python -m pytest robot/app_backend/tests -q
.venv/bin/python -m pytest robot/robot_backend/tests -q
.venv/bin/python -m pytest robot/simulation/tests/test_bridge.py robot/simulation/tests/test_speech.py -q
.venv/bin/python robot/contract/export_schemas.py --check
node --check robot/frontend/app.js
node --check robot/simulation/web/app.js
.cache/dimos/.venv/bin/python robot/simulation/tests/test_locomotion.py
.cache/dimos/.venv/bin/python robot/simulation/tests/test_navigation.py
```

Brain tests need their declared dependencies; physics commands require pinned
assets and the simulation environment. Native macOS speech smoke checks cannot
be inferred from a Linux run. Separate mocked speech tests from the real-device
smoke test where required; a skip must identify the missing coverage. Running
these existing commands alone does not cover all target requirements above.

Routine CI must not make paid provider calls, contact real recipients, start
physical movement, or depend on credentials. Opt-in image/audio/hardware checks
produce separate reports tied to the same code revision.

## 15. Evidence bundle, signoff, and delivery order

Use [ACCEPTANCE_RUN_TEMPLATE.md](ACCEPTANCE_RUN_TEMPLATE.md) for each qualification.
Keep local artifacts under `output/acceptance/<run-id>/` or another explicitly
ignored path. Commit a sanitized small summary if useful; never commit keys or
raw resident media. A file path in a report is not evidence until the file exists
and its contents match the claimed run.

Minimum evidence:

1. Frozen profile with code/config/schema/model/prompt/scene/asset revisions.
2. Requirement-by-requirement result table with counts, reasons, and evidence.
3. Timeline for each full scenario: capture → inference → policy → question
   start/end → utterance capture/recognition → escalation → UI → acknowledgment
   → family message playback. Include IDs and both relevant time domains.
4. Event/state/command/delivery journal, sanitized errors, test output, and metrics.
5. Actual rendered camera samples, measured trajectory/contacts, UI capture,
   and audio evidence appropriate to the claimed mode.
6. Every failed/blocked/not-run case, owner, next action, and affected claim.
7. Henry/Roger/Ellis/Sam component signoff plus integrated-profile signoff.

The final demo decision is binary for each gate. No composite percentage can
hide a failed invariant. A failure after signoff reopens the affected gate.

Implement in this order:

1. Freeze one API/profile and remove misleading live/mock capability behavior.
2. Make incident state/effects durable; extract the deterministic policy seam.
3. Fix playback-aware timing, communication-failure reasons, reply provenance,
   and no-rearm-on-occlusion behavior with G0 tests.
4. Qualify actual local image input and stale-result handling on the chosen model.
5. Qualify the actual output/input path and connect correlated reply handling.
6. Run the core family loop, then repeat/restart/fault tests.
7. Run held-out evaluation and soak qualification; fix causes, retain failures.
8. Add external notifications or physical hardware only under their own gates.

## 16. Inspection baseline and known gaps

This section records the files inspected while drafting on 2026-09-19, around
commit `cb1b558` plus concurrent uncommitted work. It is a snapshot, not a live
status board. New edits may already address a gap; verify before implementing.

| Finding | Evidence at inspection | Acceptance implication |
| --- | --- | --- |
| Question timer starts when queued. | `robot/app_backend/app/service.py` creates `deadline_at=now+8000` immediately after suspicion. | POL-13/14 and playback failure distinction require a target change. |
| Pending incident, episode flag, maps and command list are in memory. | `Service.__init__`; SQLite currently persists events and memory. | Restart preservation/outbox/recovery not established by existing event persistence tests. |
| Empty confident observation can re-arm an episode. | `observe()` treats `not frame.person` as safe when no pending check-in. | POL-24 requires affirmative recovery evidence instead. |
| Two distinct UUIDs can share one timestamp and trigger the demo. | Existing scenario helper and equal-timestamp policy test. | Real capture provenance/separation is a new target; retain fixture-only behavior only in explicitly isolated G0 helpers. |
| Event evidence stores one frame reference. | `Event`/`Evidence` models and second-frame selection. | Store the qualifying pair and playback/reply correlations through a schema migration. |
| Transcription returns text without confidence. | `robot/contract/audio.md`, brain audio implementation. | Validated reply-quality bridge remains necessary; never invent a score. |
| Saved live audio attempt reported missing audio. | `output/vision/first-audio-result.json`. | This is a failed modality test, regardless of HTTP success; later implementation must be retested. |
| Local vision sample has contradictory output. | `output/vision/benchmark_r2.json`: no person with standing posture. | Cross-field consistency and held-out usefulness remain unproven. |
| Cloud camera sample exceeded freshness limit. | `output/vision/mimo-camera-result.json`: approximately 28.2 s. | Useful description does not qualify as timely incident evidence. |
| Walking uses a matched Go1 surrogate. | `robot/simulation/LOCOMOTION.md`, measured validation artifact. | G1 movement evidence does not validate Go2 hardware or full DimOS stack. |
| Alternative Swift mock falls back to hardcoded data. | `app_frontend/README.swift`. | Live integration must use explicit modes and one authoritative API. |
| CI primarily covers the app backend. | `.github/workflows/checks.yml` lacks the brain/audio/bridge behavioral suites at inspection. | ENG-08 requires expanded coverage without paid calls/hardware. |
| Some docs lag new integration code. | Audio body limit note vs route-specific middleware; older readiness descriptions. | Reconcile after implementation; do not assume a documented blocker still exists. |

No application, simulation, hardware, model, or live acceptance suite was run
as part of writing this specification. Existing evidence is context only;
all newly specified acceptance targets start NOT RUN until demonstrated.

## 17. Traceability to the existing product and simulation specifications

| Existing requirement | Detailed acceptance coverage |
| --- | --- |
| REQ-001 map/status/waypoints | ROB-01 through ROB-08; NAV-01 through NAV-08; APP-07; HW-06. |
| REQ-002 validated perception | INV-02 through INV-05; CON-01 through CON-04; VIS-01 through VIS-11; POL-05 through POL-12. |
| REQ-003 sustained concern/check-in | POL-01 through POL-15; AUD-01 through AUD-05; E2E-01/02. |
| REQ-004 reassurance/help/timeout | POL-16 through POL-31; AUD-06 through AUD-12; E2E-03 through E2E-07. |
| REQ-005 two-way communication | CON-03/09; APP-02 through APP-06; AUD-01 through AUD-04/11/12; NOT-01 through NOT-05 when claimed. |
| REQ-006 cited memory | MEM-01 through MEM-07; E2E-08; temporal-memory stretch criteria if that separate claim is made. |
| REQ-007 local frame boundary | INV-12; CON-10; VIS-09; MEM-07; section 11. |
| REQ-008 reproducible demo | All G0 checks; E2E-10; sections 9, 14, and 15. |
| REQ-009 optional advisory team | POL-32; section 13's advisory criteria; existing mocked provider tests. |
| REQ-010 simulated motion | ROB-01 through ROB-08; NAV-01 through NAV-08; E2E-09. |
| REQ-011 image/audio compute boundary | CON-01/02/07/08; VIS-01 through VIS-11; AUD-06 through AUD-10; FLT-01/07; section 4.4. |
| SIM-001 through SIM-003 | G1 robot/navigation qualification and G-DIMOS only when claimed. |
| SIM-004 through SIM-012 | G0 policy matrix plus actual G2 image/audio scenario scripts. |
| SIM-013 through SIM-017 | APP-05 through APP-07; MEM tests; CON-10; FLT tests; media/provider boundaries. |

The strengthened playback, persistence, timing, and recovery targets supersede
older target prose where inconsistent, as linked from `SPEC.md`. Actual wire
compatibility remains governed by the deployed contract until migrated. An
implementation handoff names the acceptance IDs, not simply “fall detection done.”
