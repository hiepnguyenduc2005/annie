# HackMIT 2026 working brief

Captured from the team's notes on 2026-09-19. This preserves ideas and planning
history; the current build scope lives in [SPEC.md](../../SPEC.md). Advertised
sponsor inventory and services are not evidence that the team has access yet.

## Source register

- [Sponsor credits, original pasted text](sources/sponsor-credits.txt).
- [Latest proposed contract and ten-hour plan, original pasted text](sources/proposed-contract.txt).
- [Complete sponsor inventory, links, codes, and ambiguities](SPONSORS.md).
- Named references without supplied document URLs: **HackMIT 2026 Prizes.docx - Google Docs**, **HackMIT 2026 Challenges - Google Docs**, and **HackMIT 2026 Sponsor Credits.docx - Google Docs**. Their complete contents have not been retrieved.
- The conversational notes summarized below are team proposals, not independently verified product or medical claims.

## Product direction and prioritization history

Annie is the company/product name. A home robot dog intermediates between an
elderly resident or patient and the family or other people who care about them.
The first priority is a working API contract and two-way app/dog communication;
the first proposed use case is an emergency check-in after seeing a person
lying somewhere other than a bed. Treat posture as an uncertain observation,
not proof of unconsciousness or a medical diagnosis.

Two directions matter: the dog initiates a check-in/alert, and a concerned
relative initiates a message through the dog. Both manual and automatic
triggers were discussed. The team explicitly asked to keep the roughly
ten-hour build narrow and emphasize video processing.

Earlier levels were: (1) two-way intermediary, (2) disaster response/video,
(3) on-device operation, and (4) unspecified. The latest pasted plan expands
the demo to mapping/patrol, local perception, alerts, voice, and cited memory.
Its explicit cut list takes precedence over the broader ideas below.

## Earlier ideas retained for later consideration

- Resident/user profiles; a user page, reminder list, and SOS control.
- Initial home mapping, originally including two floors; visual map in the app.
- Scene understanding and memory: user profile, spatial and temporal context,
  a proposed "4D spatiotemporal knowledge graph," cited frames and map pins.
- Audio/video processing and speech generation; communication from dog to app
  and app to dog; communication between the backend and ASUS compute.
- Care-center synchronization, medical staff/family notification subscriptions,
  and a hospital admin dashboard.
- Heat/air/environment sensing if useful; Voloridge air-quality data and
  Regeneron clinical datasets as possible specialized directions.
- Immediate reminders for time-sensitive tasks; gentle reminders for daily
  routines; reminders to walk/eat; adapting to preferences, missed tasks,
  and observed spatial habits. "Psychological reinforcement" is an idea,
  without an established clinical effect.
- Meta-related idea: connect elderly people with each other.
- Local AI preprocessing to protect PII before anything is sent online;
  "air-gap LLM process" was considered, but cloud voice, search, and messaging
  are incompatible with a fully air-gapped end-to-end system.
- Swift versus React Native was undecided; the latest proposal permits a web
  interface if React Native delays the demo.
- Ansys only if a specific engineering calculation becomes necessary.
- The source had unfinished headings for Dog, Air-gap, App, Audio, Features,
  and Communication; these do not establish additional requirements.

## Team responsibilities supplied in the notes

| Workstream | Named owner | Initial responsibilities |
| --- | --- | --- |
| Mobile app / frontend | Sam | Broad app structure and family interface. |
| API and database | Ellis | Backend, data, and shared API contract. |
| Local agents and communication | Roger | Local processing, communication, Deepgram integration. |
| Dog setup and networking | Henry | Hardware setup and network path. |

The later plan groups work as Body, Brain, Voice + backend, and App + demo.
It calls Brain's owner "you" without a name; do not silently reassign the
named team. Coordinate integration points through the contract.

## Latest intended demo

1. DimOS maps one room/flat and patrols three waypoints.
2. A GX10-local vision model classifies person/posture with confidence. The
   proposed rule exempts the bed and checks in for sustained non-bed lying.
3. An alert reaches the family app with evidence and a map pin; Linq is the
   intended SMS/iMessage adapter.
4. The dog asks "Grandma, are you OK?"; Deepgram handles speech input and
   ElevenLabs speech output. Family can send a message through the dog.
5. Captions with timestamps and the observer's map pose support retrieval,
   with evidence citations. Elastic is the intended external index.

Stretch after this loop works: spoken time-based reminders and routine drift
(example: no kitchen observation since 9 am). Cut for this demo: two floors,
multiple profiles, hospital dashboard, air-quality sensing, Arduino work,
robot arm, and fine-tuning.

## Hardware, accounts, and demo set

| Item | Intended use / qualification |
| --- | --- |
| Unitree Go2 | Dimensional booth loan; checkout still needs confirmation. |
| DimOS SDK and MuJoCo | Evaluate the existing simulator before physical integration. |
| Ubuntu 22.04/24.04 laptop or vendor Docker setup | Intended robot/DimOS host; notes call macOS alpha, which needs version-specific verification. |
| ASUS Ascent GX10 | Intended local vision-language inference. Credits say Nemotron is preinstalled, but the exact model and image-input support need a one-frame test. |
| Second laptop | Voice pipeline and demo screen. |
| ASUS ZenScreen | Simulator view and measured demo metrics. |
| Bluetooth/USB speaker secured to dog | Notes suggest hall audio may exceed the built-in speaker's volume; verify setup. |
| USB push-to-talk button or phone | Resident speech input. |
| Phone/tablet | Family app, alert, check-ins, cited "ask it" results; a continuous live camera was an earlier idea and is not assumed by the current crop-only API. |
| Second phone | Alert/notification demonstration. A notification is not a completed voice call. |
| Phone hotspot | Network fallback. A cloud VLM fallback would change the local-only frame policy and is not enabled by default. |
| Deepgram / ElevenLabs / Linq credentials | Intended voice and notification integrations; never put actual credentials in Git. |
| Elastic trial | Intended caption-memory index; captions can also contain personal information. |
| Chair, rug or mat, bedside lamp, small table | Staged home scene. |
| Bowl and food prop | Possible "did mum eat?" memory question; observation cannot establish actual consumption. |
| Gaffer tape, extension lead, power strip | Demo setup; notes say no soldering needed. |
| Spare Go2 battery / two batteries if available | Ask the lending booth; not confirmed stock. |
| Blindfold | Mentioned explicitly as a prop that will not be used. |
| Optional UNO Q + Movement Modulino | Wrist no-motion cross-check; later cut. Notes caution against claiming a primary Arduino/Touch Grass entry without actually qualifying. |
| Optional ESP32-S3-Box | Bedside "I'm fine" voice endpoint when robot is away; deferred. |
| Anvil OpenYAM arm | Advertised lending hardware; cut unless idle after hour 8. |

The claim that GX10 units will go in the first hour is a planning concern, not
verified availability. The latest notes say to secure robot/GX10 early, test
one frame within 30 minutes, and avoid unnecessary model-server installation.

## Relative ten-hour plan from the source

| Target from team start | Gate |
| --- | --- |
| First 30 minutes | Check hardware and image-capable local model with one frame. |
| Hour 1.5 | Brain returns valid perception JSON for a webcam frame. |
| Hour 2 | Body publishes map and frames to the local bus. |
| Hour 3 | Status/events API works; app renders a mocked event; voice/backend integration progressing. |
| Hour 5 | First full end-to-end run. |
| Hour 8 | Feature freeze. |
| Final two hours | Video, README, submission fields, and three rehearsals. |

Use mocks until integration gates pass. No timestamps in these notes establish
when the team's ten-hour clock started.

Opening line: **"Welcome to grandma's house."** The source proposes the phone
ringing 60 seconds later. Display only measured metrics: time from staged
incident to notification (or call only if an actual call exists), and false
alerts over a stated set of negative scenarios. Do not invent a false-alarm rate.

## Technical evidence and unresolved integration

DimOS source inspected at repository commit
`c1c3cdc9d2ee54ca72259465688395699d7d99a2` on 2026-09-19:

- Its [experimental entity graph](https://github.com/dimensionalOS/dimos/blob/c1c3cdc9d2ee54ca72259465688395699d7d99a2/dimos/perception/experimental/temporal_memory/entity_graph_db.py)
  stores entities, timestamped relationships, and distances in SQLite. It can
  estimate distance with a VLM; such estimates are not calibrated depth.
- The [temporal-memory module](https://github.com/dimensionalOS/dimos/blob/c1c3cdc9d2ee54ca72259465688395699d7d99a2/dimos/perception/experimental/temporal_memory/README.md)
  and [navigation pipeline](https://github.com/dimensionalOS/dimos/blob/c1c3cdc9d2ee54ca72259465688395699d7d99a2/docs/capabilities/navigation/deep_dive.md)
  supply useful components, but their fusion for Annie remains to be tested.
- The [repository README](https://github.com/dimensionalOS/dimos/blob/c1c3cdc9d2ee54ca72259465688395699d7d99a2/README.md)
  documents a Go2 MuJoCo simulation. This is the first evaluation route.

A caption associated with robot `(x, y, t)` is a timestamped observation from a
robot position. It is not a measured person position, persistent object
identity, a full 3D-plus-time graph, or causal reasoning. Preserve map IDs,
frame IDs, capture timestamps, and uncertainty when joining these streams.
