# Model-led exploration

The AI control accepts an operator goal rather than a sequence of destinations.
Each turn supplies a fresh robot-camera image, the navigable map, completed
arrival receipts, incident outcomes, and cited historical observations. The
model selects `goto`, `look`, `say`, `wait`, or `stop`; Annie does not insert a
room-by-room route into this path.

Completed visits mean measured waypoint arrivals. They do not establish that an
entire room was inspected. Memory poses locate the camera, not the observed
person. Historical captions cannot establish current presence. Scene labels
and the resident's authored daily activity are excluded from model context.

An active motion can be observed and interrupted by a model-selected stop.
Another destination waits for a terminal receipt. Pausing AI suspends model
requests while retaining command and voice delivery. A resolved incident
returns to model planning instead of indefinitely requesting confirmation
images. The incident rules remain responsible for correlated check-in replies
and alert delivery; these are not decisions made by the language model.

Use **Start AI / resume** with an open-ended goal such as “Explore the ground
floor and find the resident using camera evidence.” The separate full-house
demo button deliberately stages an incident and replays recorded speech.
Stair traversal is unsupported by the current walking controller.

## Memory and cost bounds

The default retrieval uses SQLite observations. It retrieves recent person
sightings and spatially diverse views before acquiring the current image.
The optional Elastic adapter remains separately configured. Captions keep
their capture ID, timestamp, and pose; cross-map and future captures are
excluded. Completed destinations are recovered from the viewer's retained
execution receipts after a bridge restart, within the current map.

Cloud calls reserve a conservative amount before transmission. Successful,
validated responses can settle that new reservation to the provider's reported
cost. Historical reservations and uncertain calls retain their debit. A
reservation can be settled only once, with locking across processes; settlement
never resets lifetime call counters. Missing cost is not zero cost.

`--agent-max-inferences` sets a session ceiling (default 20, maximum 1000).
It does not override the brain's lifetime call or dollar limits. Continuous
local mode remains available, but the small local VLM has exceeded the existing
five-second capture validity window under this machine's load. Cloud vision
currently provides the demonstrated latency. Never restart or erase the ledger
to obtain additional allowance.

## Acceptance

Verify open-ended exploration separately from the scripted incident demo:

- Model-selected destinations produce completed movement receipts.
- A subsequent decision uses completed visits and cited observations.
- A handled incident returns to planning without reissuing its check-in.
- Model choices during motion respect the existing capture-age and execution
  gates; stale decisions are rejected rather than re-dated.
- Provider failure and an exhausted budget stop new inference while body and
  voice delivery continue. Report the failure in the frontend.

The frontend must distinguish selected, queued, executed, and rejected actions.
A plausible explanation alone is not evidence of intelligence or execution.
