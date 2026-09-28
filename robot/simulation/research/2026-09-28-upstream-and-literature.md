# Research review — 28 September 2026

## Scope and baseline

Read primary paper methods, experiments and limitations, plus the selected DimOS
implementations and tests linked below. This review changes documentation only:
no services, dependencies, simulation behavior, network settings or hardware
were changed. No paper or upstream benchmark was reproduced.

- Annie revision: `057747d0e3140384b9eb5213412de42e2baaaf1f`.
- DimOS source currently attributed in Annie's LiDAR adapter:
  `c1c3cdc9d2ee54ca72259465688395699d7d99a2`.
- Reviewed DimOS `main`: `edd7c346b8158a406c915dfe922202dc2d652e21`,
  committed 25 September 2026, 22:14:53 UTC. The [comparison][compare] reports
  20 commits ahead, zero behind. Its changed-file response contains the GitHub
  maximum of 300 files; this is a focused source audit, not exhaustive coverage.
- Compared with [prior art](../../../docs/PRIOR_ART.md), which already covers
  ReMEmbR, Embodied-RAG, Meta-Memory, MindPalace, KARMA and SemanticFlip.

Current implementation matters when interpreting these findings:

| Annie component | What exists at the recorded revision |
| --- | --- |
| [Spatial sensor](../spatial.py) | 720 MuJoCo rays, at most 5 Hz, default 12 m range, robot subtree excluded; simulated hit points and measured simulated trajectory for the operator. No scan-derived occupancy map. |
| [Navigation](../navigation.py) | A* over authored collision geometry, 0.12 m grid and 0.38 m obstacle inflation. It does not navigate from the displayed LiDAR cloud or perform SLAM. |
| [Graph memory](../local_graph_memory.py) | Frame-bound observations, captions, timestamps and observer poses; `OBSERVED_IN` edges to maps; capacity rejects new observations at 1,000 per map. This is not persistent object identity or an activity graph. |
| [Working context](../../robot_backend/app/brain/context.py) | 12,000-byte text budget; eligible memories packed newest first while preserving complete citations. |
| [Image evidence](../evidence.py) | Separate synthetic JPEG evidence cache, default 64 frames, evicted by file modification time. A retained graph citation need not imply its JPEG remains cached. |
| [Completion](../goal_completion.py) | Current-goal speech completion already depends on scoped delivery evidence; stale, missing and contradictory receipts cannot simply establish success. |

The isolated `demo_sim.py` Go2 rehearsal and the trained Go1 surrogate physics
viewer are different simulation paths. Neither constitutes a physical Go2
qualification. The separate family-relay lost-phone workflow remains incomplete;
a historical viewer demonstration does not establish its completion.

## 1. DimOS: useful code changes since the attributed revision

The repository declares [Apache-2.0][license]. The reviewed Python files also
carry Apache-2.0/Dimensional headers; `Map3DPanel.tsx` has no file-level license
header, and the inspected tree has no separate web license. No code was imported
in this review. Any later adaptation should retain applicable notices and record
modifications; these findings do not establish licenses for transitive packages.

### Local planning and stale-map handling

Commit [e5679069df7dbe1630972911a0715370247f1953][planner-commit], 21 September.
The [planner][planner] records monotonic map **arrival** time and, with a usable
pose, clears its incumbent plan and publishes a hold when map age exceeds the
configured threshold (default 5 seconds). [Tests][planner-tests] check that the
single-pose hold produces zero controller velocity, clearing a route happens
once, and stale pose data cannot generate another plan.

This is a useful pattern for a future scan-derived planner. Arrival age does not
prove capture freshness: repeated delivery of an old scan could still appear
live. Nor does the stale-pose test establish that an already executing controller
has stopped. Annie should test both capture and arrival age, controller timeout,
and outstanding-plan cancellation together. Do not copy the five-second value
as an appropriate operating threshold. The upstream [body-band obstacle
filter][obstacles] is also worth comparing against synthetic ground/person
geometry; its `path_clearance` explicitly describes a speed hint rather than a
safety contract.

**Integration opportunity:** an offline scan-to-occupancy experiment using
Annie's existing synthetic rays, with authored geometry retained as an evaluation
oracle. Measure free/occupied/unknown classification, false free cells around
people, and behavior when map or pose updates cease. This would be new work;
the current visual trace is not evidence of mapping.

### Replay measurements before a runtime upgrade

Commit [beaaff4865eb1be96ea6366862e59eb27b6270bb][replay-commit], 24 September.
The [replay benchmark][replay] derives expected message counts from the recording,
checks delivered odometry/LiDAR/color frames, and records startup, CPU, memory,
thread and disk measurements. Its floors are 90%/90%/50%, respectively; these
are upstream test choices, not Annie acceptance criteria or measured results.

**Highest-priority reuse:** adapt the measurement pattern to Annie's own finite
synthetic replay with fake inference/audio providers. Inject delayed, duplicate,
dropped and out-of-order frames and receipts. Report delivered counts, source age,
capture-to-decision and command-to-receipt latency, peak memory, and false task
completion. Keep source timestamps, map IDs and goal revisions in the trace.

Do not directly run this upstream harness on the operator's Mac: it requires
Linux cgroup accounting, is marked for an 8+ GB runner, and is explicitly skipped
for macOS RPC/transport issues. Its comments mention host route/buffer tuning;
this review neither requires nor performs that tuning. Replay of robot recordings
also remains replay evidence, not a new hardware trial.

### Rendering and navigable map presentation

- Commit [521b424bfc91197c4de15dfeb85b28fee92bc2c8][shadows-commit], 21 September:
  [MuJoCo shadow selection][shadows] warms up three renders, measures five, and
  disables shadows when rendering exceeds 30% of the video frame budget.
  `shadowsize=0` is applied before renderer/viewer GL context creation. Benchmark
  this on a fixed Annie trajectory before adoption; camera appearance and visual
  inference can change. The upstream speedup comment is not an Annie measurement.
- Commit [f2945fad060082dae0023f4f00cade270b097c13][voxels-commit], 23 September:
  [voxel codec][voxels] encodes occupied point bins in compressed 16³ chunks,
  doubles resolution when voxel/chunk budgets are exceeded, and transmits the
  actual resolution. [Golden-fixture tests][voxel-tests] cover the encoding.
  The [Map3D sink][map3d] decodes one frame at a time, skips to the newest,
  preserves the user's orbit after initial framing, clears an empty map, and
  pauses hidden documents.

The map sink's interaction and backpressure patterns are practical references
for the draggable scene. A compressed voxel transport is conditional on much
larger accumulated maps; 720 rays alone do not justify it. Quantized hit occupancy
does not supply probabilistic free/unknown mapping. Keep sensor hits, observed
map and planned route visibly distinct.

## 2. Recent primary papers

All numbers below are **authors' reported results**, not Annie results. Full
versioned paper text was read. No licensed implementation for these three papers
was established from the checked paper links; a paper's license is not a code
license. Their methods are evaluation/design candidates, not imported software.

### Dynamic objects and limited context

Vishnu Sashank Dorbala and Dinesh Manocha, **Deploying Foundation Models for
Embodied Navigation**, [arXiv:2609.25666v1][deploy-paper], 22 September 2026.
Paper license: CC BY 4.0. Read §4.1–4.2, Tables 2–3 and qualitative failure cases.

Transit-Aware Planning (TAP) models portable objects moving between locations
over time. Its physical TurtleBot lab study reports routine-condition success
of **80% versus 57.5%** for the LLM baseline; Table 2 describes averages over
50 episodes. This bounded lab result does not establish transfer to Annie,
Go2 motion or resident behavior.

The separate MemCtrl experiment is **simulation**: a detachable memory-selection
head over Qwen2.5-VL-7B-Ins. Across three trials and five EB-Habitat subsets,
Table 3 reports average success **21.0% versus 15.2%** for offline supervised
MemCtrl versus the baseline. The RL variant scores 18.4%, so extra training is
not uniformly better. Figure 8 also shows premature completion and repetition;
Annie should preserve its external completion checks.

**Concrete next experiment:** move the simulated phone after a valid sighting,
then occlude it or place a similar object at the old location. Score separately
whether Annie can answer “where was it last seen?” with a timestamp, admit that
its current location is unknown, and verify a new sighting. A routine prior may
rank search locations; it must not become an observed current location.

### Selecting episodes under a fixed memory budget

Nicolas Gorlo et al., **Worth Remembering: Surprise-Gated Robot Episodic Memory**,
[arXiv:2606.03787v3][surprise-paper], revised 6 June 2026. This older paper is newly
reviewed here, not a publication from this week. Paper license: arXiv perpetual
non-exclusive distribution license. Read §3–5 and Table 1.

The causal gate computes surprise from V-JEPA-2 latent features using a rolling
Gaussian approximation, a median/MAD threshold and peak suppression. On seven
recorded CODa sequences in OC-NaVQA, with GPT-5-mini for all comparisons and
three seeds, question accuracy is **0.796** for surprise episodes, **0.761** for
budget-matched uniform/random episodes, and **0.711** for DAAAM alone. The latter
gain is 8.5 percentage points, approximately 12% relative. Authors retain 1.7%
of frames, averaging 1.28 episodes/minute, but report **13% more reasoning
tokens** and require multimodal retrieval reasoning. Sparse storage is not
automatically cheaper inference. Reappearing surprises can repeatedly trigger
storage; the paper also notes limitations of its Gaussian surrogate.

**Concrete next experiment:** compare current recency packing, uniform selection,
and inexpensive semantic novelty at identical stored-frame and context-byte
budgets before evaluating V-JEPA-2. Measure answer/citation correctness, surviving
JPEG evidence, latency and inference cost. Reserve evidence for goal boundaries,
object/person sightings and execution failures independently of novelty. Memory
selection must not gate person-stop logic or incident timing. The paper motivates
selection; it does not resolve Annie's graph/image retention mismatch by itself.

### Human activity requires temporal and object grounding

Ermanno Bartoli et al., **GESTO: Human-Centric Spatio-Temporal Memory for Reasoning
in Dynamic Scenes**, [arXiv:2608.10886v1][gesto-paper], 11 August 2026. Paper
license: arXiv perpetual non-exclusive distribution license. Read §IV–VI and
the main/ablation tables. The text promises code “upon acceptance”; availability
and an implementation license were not established in this review.

GESTO adds atomic interactions and grouped events to a persistent 4D scene graph.
Its 15-second RGB-D clips, Cosmos-Reason2 extraction and object-mask grounding
are substantially more than a caption-to-map edge. Unmatched interactions can
remain unlinked, and context-based refinements require unambiguous candidates.
This supports retaining the distinction between observed geometry and inferred
activity associations.

Reported text-answer score is **0.71**, and temporal-query score is **0.70**
(versus 0.40 without the event hierarchy). Text scoring uses an LLM judge;
temporal answers allow ±2 minutes. The authors exclude the original benchmark's
node category because required ground truth is unavailable and add 40 manually
authored spatial/event queries. Egocentric results are qualitative, not a
quantitative deployment trial. Occlusion, similar objects, bad masks and memory
growth remain explicit limitations.

**Concrete next experiment:** distinguish “phone visible beside person,” “person
placed phone,” and “person used phone.” Include negatives with occlusion and
similar objects, requiring interval/frame citations or abstention. Do not infer
intent or task compliance from proximity. An activity graph can follow only
after object tracking and temporal evidence support these claims.

## Recommended integration order and verification

1. **Offline replay and failure accounting:** reuse the DimOS measurement pattern;
   ensure missing/stale streams and receipts cannot produce false completion.
2. **Moved-object and interaction fixtures:** test historical versus current
   location, occlusion, similar objects and unsupported activity claims.
3. **Budget-matched memory selection:** retain comparable evidence and measure
   quality/cost before adding encoders or trained memory heads.
4. **Conditional rendering/mapping work:** benchmark shadow policy and map sink
   behavior; evaluate ray-derived occupancy separately from authored navigation.

These proposals do not change SPEC/TODO requirements. This pass verified source
revisions, code paths, paper versions and table arithmetic. No runtime test suite,
paid inference, replay benchmark or physical robot experiment was run; there is
no measured Annie performance gain to report.

[compare]: https://github.com/dimensionalOS/dimos/compare/c1c3cdc9d2ee54ca72259465688395699d7d99a2...edd7c346b8158a406c915dfe922202dc2d652e21
[license]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/LICENSE
[planner-commit]: https://github.com/dimensionalOS/dimos/commit/e5679069df7dbe1630972911a0715370247f1953
[planner]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/navigation/local_planner/module.py#L215-L261
[planner-tests]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/navigation/local_planner/test_module.py#L107-L189
[obstacles]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/navigation/local_planner/obstacles.py
[replay-commit]: https://github.com/dimensionalOS/dimos/commit/beaaff4865eb1be96ea6366862e59eb27b6270bb
[replay]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/robot/unitree/go2/test_replay_benchmark.py
[shadows-commit]: https://github.com/dimensionalOS/dimos/commit/521b424bfc91197c4de15dfeb85b28fee92bc2c8
[shadows]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/simulation/mujoco/mujoco_process.py#L73-L145
[voxels-commit]: https://github.com/dimensionalOS/dimos/commit/f2945fad060082dae0023f4f00cade270b097c13
[voxels]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/web/relay_bridge/builtin_codecs.py#L147-L221
[voxel-tests]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/dimos/web/relay_bridge/test_voxel_encoding.py
[map3d]: https://github.com/dimensionalOS/dimos/blob/edd7c346b8158a406c915dfe922202dc2d652e21/web/cockpit/src/panels/Map3DPanel.tsx#L61-L150
[deploy-paper]: https://arxiv.org/html/2609.25666v1
[surprise-paper]: https://arxiv.org/html/2606.03787v3
[gesto-paper]: https://arxiv.org/html/2608.10886v1
