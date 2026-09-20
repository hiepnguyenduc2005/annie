# Annie specification

Status: Working HackMIT demo scope, 2026-09-19. Requirements describe targets,
not completed hardware or service integrations. The [team brief](docs/hackmit-2026/NOTES.md)
retains earlier ideas and source notes.

## Product and first workflow

Annie connects one elderly resident at home with family through a robot dog.
A possible incident prompts a check-in, followed by a family alert if help is
requested or reassurance does not arrive. A relative can acknowledge the alert
and send a message through the dog. This is a staged assistance prototype,
not a medical diagnosis or a replacement for emergency response.

## Version 0 requirements

| ID | Behavior | Acceptance check |
| --- | --- | --- |
| REQ-001 | One-floor map, status, and three waypoints. | App displays map ID and robot observation position; physical patrol verified separately. |
| REQ-002 | Validated timestamped perception with evidence IDs. | Invalid, stale, duplicate, unknown, timed-out, or low-confidence input cannot create an incident; bed is exempt. |
| REQ-003 | Sustained non-bed lying initiates a check-in. | Two distinct fresh floor/chair observations start one eight-second check-in; continuing ticks do not flood alerts. |
| REQ-004 | Help or timeout escalates to family attention. | Confident correlated reassurance closes the check-in; ambiguous speech does not; help or timeout escalates once. |
| REQ-005 | Two-way communication. | Dog events reach app; family messages enter identified voice commands; queue acceptance is distinct from playback/delivery. |
| REQ-006 | Evidence-backed scene memory. | Query cites frame ID, capture time, and observer pose; no evidence gives an explicitly unanswerable response. |
| REQ-007 | Full frames remain local. | Raw frames do not enter the app stream, cloud memory, cloud agents, or routine logs. Released derivatives have explicit egress policies. |
| REQ-008 | Reproducible software demo. | Fresh checkout exercises bed, incident, reassurance, timeout, acknowledgement, message, and query without keys, hardware, or outbound notifications. |
| REQ-010 | The simulated dog executes waypoint, patrol, stop/resume and look missions. | Trained-policy joint actuation moves the matched Go1 surrogate; measured poses and command receipts reach the app. |
| REQ-011 | Rendered image and synthetic audio inference use interchangeable compute services. | Preserve capture identity and timestamps, validate model outputs, report failures, and enforce the configured shared cloud budget. |
| REQ-009 | Optional Subconscious advisory team. | Bounded text-only agents run only when configured and opted in; no robot, alert, or messaging authority; provider failure is tested with mocks. |
| REQ-010 | Asynchronous family message relay to the resident, with live progress. | Posting a message returns a run ID immediately without waiting on the robot; an unreachable robot and normal progress both surface as run events, never a blocked request or a 500; connected family clients see live thread and run updates over WebSocket. |
| REQ-012 | SwiftUI app registers an app user and a dog user profile and records which was created first. | An app user registering creates both profiles, linked; a dog user can register alone; blank, over-50-character, and repeat registrations are rejected. Profiles are stored on the device only; server-side storage, cross-device pairing, and who may create whom remain open (DEC-008). |

The rule uses known floor/chair locations. Unlike the source's literal
`location != bed`, unknown location requires more evidence. Confidence
thresholds are demo settings, not clinical performance. `fall_confirmed` is
retained as a wire name for escalation confirmed, never a proven medical fall.

## Scope boundaries

Target integrations: Go2 with DimOS/MuJoCo, GX10-local image-capable Nemotron,
Deepgram, ElevenLabs, Linq, and Elastic. Track each as simulated/disconnected
until verified. Initial delivery is a phone-friendly web app and local backend;
React Native remains an option after the end-to-end workflow works.

Stretch: time-based spoken reminders and routine drift. Deferred: two floors,
multiple profiles, hospital dashboard, air-quality sensing, Arduino, robot arm,
fine-tuning, social matching, and Ansys without a specific engineering need.

## Evidence, data, and privacy

A robot pose is not a person's measured location. Preserve map ID, frame ID,
capture timestamp, and uncertainty. DimOS offers useful navigation and memory
components, but their fusion for Annie remains an integration task; captions
with coordinates do not establish persistent identity or a full 4D graph.

Full frames stay on the trusted local body/brain network. Cloud audio/text,
Elastic captions, Linq notifications, released crops, and Subconscious evidence
can contain personal information. This is local-first, not fully air-gapped.
The user authorized cloud inference for synthetic simulator frames on 2026-09-19, within a total $20 external inference budget. It is explicitly configured; real resident/hardware frames remain local. No automatic provider fallback is permitted.
Use synthetic data for the current demo; define retention and crop release
before collecting actual resident observations. Never put media or secrets in Git.

## Interfaces and open integration questions

[The contract](robot/contract/README.md) defines component interfaces. Resolve the
actual hardware/SDK/model versions, frame-to-pose synchronization, crop and
retention policy, named owner for brain integration, and notification/playback
acknowledgements before physical signoff. Measure demo latency and false alerts
on stated scenarios instead of inventing performance figures.

Manual Go2 control must clear held input and attempt neutral input plus a
stand-preserving StopMove before disconnect, navigation away, or loss of page
focus. Returning focus alone must not resume motion. The separate damping
control must clearly say that it relaxes the motors. Hardware commissioning
must follow the manufacturer's battery operating guidance; small odometry
changes and command acknowledgments do not establish a completed patrol.

## Detailed acceptance and codebase quality targets

[Acceptance target 1](docs/ACCEPTANCE.md) specifies component tests, exact
incident behavior, measurable performance targets, fault/restart recovery,
end-to-end scripts, and separate software/simulation/audio/notification/hardware
gates. [The architecture contract](docs/ARCHITECTURE.md) assigns owners and module
boundaries; [the run template](docs/ACCEPTANCE_RUN_TEMPLATE.md) records evidence.

These are target requirements, not assertions that the current implementation
passes. They intentionally strengthen the v0 demo: start the resident response
window after verified question playback, distinguish communication failure from
silence, persist active incidents/effects across restart, preserve the complete
evidence pair, and require affirmative recovery before re-arming an episode.
Where older prose describes current behavior differently, acceptance target 1
defines the intended next behavior. Existing strict wire contracts remain in
force until producers, consumers, schemas, and tests migrate together. See the
acceptance document's inspection baseline for the known implementation gaps.
