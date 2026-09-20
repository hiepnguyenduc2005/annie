# Live model-directed search and memory

Measured on 2026-09-19 using the furnished Grandma's house simulation,
`google/gemini-3.8-flash:floor`, and native macOS audio playback. These are
simulation results, not hardware or clinical validation.

## Search

The operator asked the model to find a visible person, choose its search route,
describe its observation aloud without guessing identity or health, and finish
after playback. No route or incident was staged for this run.

- The model selected hallway → study → bedroom. All three navigation commands
  received completed receipts based on measured MuJoCo robot position.
- The VLM reported a person while walking toward the bedroom and selected:
  “I observe a person standing on the floor.”
- Native audio produced a 1.77-second clip. Its playback process completed;
  this establishes local output completion, not that a human heard it.
- Early `finish` choices were rejected while navigation was still executing.
  Completion was accepted after the bedroom arrival and speech receipt.
- The task completed in 78.85 seconds using 16 inference attempts. Observed
  median provider latency was 1.826 seconds; range 1.350–13.530 seconds. The
  slow response expired under the existing five-second execution gate.
- No new inference occurred after completion until another operator goal.

Raw runtime evidence is saved locally at
`output/live-gemini38-intelligent-search.json`. The run's shared-ledger increase
was $0.04705575; older conservative reservations were preserved.

## Historical-memory follow-up

A second goal requested an account of the last person sighting from memory,
without moving. At the speaking decision, current perception was `person=false`
and described empty bedroom furniture. The separate historical sighting still
cited the earlier bedroom frame and original camera pose.

The model generated a historical bedroom description, completed a new
7.49-second native speech clip, and selected `finish`. This used two further
inferences and completed within 18.17 seconds of observation. Evidence:
`output/live-historical-memory-followup.json`.

The spoken wording said “this is not their current location”; the evidence
supports “current location is unconfirmed.” Memory retention and playback
worked, but this wording illustrates why model assertions require scrutiny.

## Limits

This validates model-selected movement, perception reporting, speech execution,
historical recall, and finite-task completion. It does not establish general
person-detection accuracy or broad autonomous reliability. The exact historical
JPEGs were not retained in these logs; they contain frame IDs, capture poses,
timestamps, model observations, and command/audio receipts. The simulator uses
the trained Go1 walking surrogate; stairs remain unsupported.
