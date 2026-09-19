# Annie acceptance run record

Copy this template into an ignored local run directory. Do not edit this blank
template to claim a result. See [acceptance specification](ACCEPTANCE.md) and
[architecture contract](ARCHITECTURE.md). Default every test to NOT RUN.

## Run identity

| Field | Recorded value |
| --- | --- |
| Run ID / start / end / timezone | TODO |
| Operator and component owners | TODO |
| Gate(s) claimed | TODO: G0 / G1 / G2 / G2-N / G3 / G-DIMOS |
| Source mode and exact demo claim | TODO |
| Code commit | TODO |
| Dirty patch hash and archived patch, if any | TODO; final signoff should use a reproducible frozen revision |
| Acceptance specification revision | TODO |
| Profile/config hash | TODO |
| Schema version | TODO |
| Model/weights/version and prompt hash | TODO |
| Hosted version availability, if applicable | TODO; record unknown if provider does not expose it |
| Simulation/SDK/model assets/scene generator revisions | TODO |
| Robot model: actual Go2 / Go1 surrogate / physical Air | TODO |
| Scene manifest/seeds/evaluation split | TODO |
| OS/CPU/GPU/RAM/dependency versions | TODO |
| Actual input/output audio devices and route | TODO |
| API/brain/viewer endpoints (without credentials) | TODO |
| Enabled notification channel and authorized test destination alias | TODO; do not publish private contact details |
| Warmup procedure and cold-start duration | TODO |
| Synthetic media retention and cleanup manifest | TODO |
| Resource caps chosen before run | TODO: process memory, disk/media cache, queues, pending effects |
| Existing authorization/budget remaining for opt-in provider calls | TODO; no new allowance implied by this form |

## Preflight

- [ ] Revision/configuration frozen; concurrent development excluded from run.
- [ ] Fresh startup follows the documented commands; required assets available.
- [ ] Source mode, robot model, map source, and audio mode visible.
- [ ] Camera and synchronized pose fresh; model actually image-capable.
- [ ] Input/output tested; selected player/session and audibility witnessed.
- [ ] Family connection healthy; durable database and journal path confirmed.
- [ ] No unexpected physical endpoint, cloud fallback, mock fallback, or recipient.
- [ ] No credentials/private media in logs, screenshots, or tracked files.
- [ ] Test destination and media route already authorized when applicable.
- [ ] All required test IDs copied into the result inventory.

## Requirement results

Create one row per applicable acceptance ID, including inherited gates. Do not
collapse a suite to PASS when individual tests were skipped or failed.

| ID | Gate | Priority | Status | Attempts / passes | Measurement or failure | Evidence path | Owner |
| --- | --- | --- | --- | --- | --- | --- | --- |
| TODO | TODO | P0 | NOT RUN | 0 / 0 | TODO | TODO | TODO |

Allowed statuses: PASS, FAIL, BLOCKED, NOT RUN. For N/A, replace status with
`N/A: excluded <named feature>` and show the narrowed claim in the final decision.
One passing retry does not erase a failed attempt; record both and the fix/revision.

## Scenario timeline

One table per scenario attempt. Use one run/incident/correlation chain. Capture
time and receipt time are different. Include monotonic offsets for elapsed-time
checks and wall timestamps for provenance; include simulation time separately.

| Stage | Wall timestamp | Monotonic offset | Simulation time | Correlation IDs | Outcome/evidence |
| --- | --- | --- | --- | --- | --- |
| First qualifying camera capture | TODO | TODO | TODO | frame/map/run | TODO |
| First inference admitted | TODO | TODO | TODO | frame/model | TODO |
| Second qualifying capture | TODO | TODO | TODO | frame/map/run | TODO |
| Second inference admitted | TODO | TODO | TODO | frame/model | TODO |
| Incident and question committed | TODO | TODO | TODO | incident/command | TODO |
| Audible question begins | TODO | TODO | TODO | command/player | TODO |
| Question playback ends | TODO | TODO | TODO | command/receipt | TODO |
| Response cutoff | TODO | TODO | TODO | incident | TODO |
| Utterance capture starts/ends | TODO | TODO | TODO | utterance/incident | TODO or verified no speech |
| STT/intent available | TODO | TODO | TODO | utterance/model | TODO |
| Reassurance/help/timeout/communication failure | TODO | TODO | TODO | incident/event | TODO |
| Family alert visibly rendered | TODO | TODO | TODO | event/client | TODO |
| External receipt, if claimed | TODO | TODO | TODO | delivery/provider | TODO |
| Family acknowledgment committed | TODO | TODO | TODO | event/ack | TODO |
| Family message queued | TODO | TODO | TODO | command | TODO |
| Family message audible start/end | TODO | TODO | TODO | command/player | TODO |

Record derived intervals, not only timestamps:

- Frame age at admission and frame/pose skew: TODO.
- Distinct capture separation: TODO.
- Second admission to incident commit: TODO.
- Question queue to audible start/end: TODO.
- Actual response window/grace and deadline overshoot: TODO.
- Intent to event; event to visible UI; actual delivery time if applicable: TODO.
- Family command to audible playback: TODO.
- Mission displacement, arrival tolerance, contacts, minimum clearance, stop
  response/displacement: TODO.

## Model/audio evaluation summary

| Metric | Numerator / denominator | Measured value | Required target | Result |
| --- | --- | --- | --- | --- |
| Concern episodes detected in time | TODO / 20 | TODO | At least 18/20 and 9/10 each category | NOT RUN |
| False check-ins on clear negatives | TODO / 30 | TODO | 0/30 | NOT RUN |
| False escalations on clear negatives | TODO / 30 | TODO | 0/30 | NOT RUN |
| Correct answerable-frame interpretations | TODO / TODO | TODO | At least 95% overall; 90% each clear category | NOT RUN |
| Unknown/unusable/stale/timeout outputs | TODO / TODO | TODO by category | Fully counted; never hidden | NOT RUN |
| Timely usable latency attempts | TODO / 100 | TODO | At least 95/100 | NOT RUN |
| Capture-to-admission latency p50/p95/max | TODO attempts | TODO | p95 at most 3 s; no policy use after 5 s | NOT RUN |
| Help recognition | TODO / 10 | TODO | At least 9/10 | NOT RUN |
| Reassurance recognition | TODO / 10 | TODO | At least 9/10 | NOT RUN |
| Negated concern recognition | TODO / 10 | TODO | At least 9/10; no false reassurance | NOT RUN |
| False reassurance on all adverse/ambiguous/context cases | TODO / TODO | TODO | Zero | NOT RUN |
| Full consecutive core cycles | TODO / 10 | TODO | 10/10 with zero P0 failures | NOT RUN |

Attach full per-attempt outputs and confusion matrix. Explain cold/warm runs,
unknown cases, excluded unanswerable views, and exact percentile calculation.
State asset/voice diversity limits. Never extrapolate a population reliability
claim from these samples.

## Faults, recovery, and soak

| Fault/run | Injection point | Expected recovery | Actual recovery | Duplicate/lost effects | Evidence/result |
| --- | --- | --- | --- | --- | --- |
| TODO | TODO | TODO | TODO | TODO | NOT RUN |

30-minute soak: TODO start/end; crashes; task/connection/queue counts;
minute-by-minute memory; storage growth; timer lateness; stale-frame drops;
unresolved effects; maximum resource values versus predeclared caps.

## Evidence index

| Artifact | Path | SHA-256 | What it establishes | What it does not establish |
| --- | --- | --- | --- | --- |
| Frozen profile | TODO | TODO | Exact configuration | Correct behavior |
| Automated result/log | TODO | TODO | Named tests on this revision | Hardware/live modality unless exercised |
| Rendered camera and pose sample | TODO | TODO | Actual pixels and capture context | Model accuracy by itself |
| Trajectory/contact telemetry | TODO | TODO | Measured simulated motion | Go2 hardware safety |
| Audio/playback record | TODO | TODO | Named synthesis/player/device result | Audibility without witness/device evidence |
| Family UI capture | TODO | TODO | Visible app state | External notification delivery |
| Event/command journal | TODO | TODO | State/effect history | Physical action without execution evidence |

## Findings and signoff

| Issue | Severity | Affected IDs/gate | Reproduction/evidence | Owner | Fix/retest revision |
| --- | --- | --- | --- | --- | --- |
| TODO | TODO | TODO | TODO | TODO | TODO |

| Responsibility | Owner | Decision | Revision/evidence reviewed | Outstanding exclusions |
| --- | --- | --- | --- | --- |
| Robot/simulation | Henry | NOT RUN | TODO | TODO |
| Vision/voice | Roger | NOT RUN | TODO | TODO |
| Policy/API/persistence | Ellis | NOT RUN | TODO | TODO |
| Family UI | Sam | NOT RUN | TODO | TODO |
| Integrated profile | Coordinating owner | NOT RUN | TODO | TODO |

Final per-gate decision: TODO.

Exact statement the team may demonstrate/claim: TODO.

Remaining failed/blocked/not-run capabilities: TODO.

Recovery plan for the demo, with any reduced claim explicitly named: TODO.
