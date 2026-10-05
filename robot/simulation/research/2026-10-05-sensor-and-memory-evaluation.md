# Research review — 5 October 2026

## Scope

Compared primary papers and released source with Annie at
`7021c45cd3e2014b2303e386dcef35c6f3dc1d18`, following the
[28 September review](2026-09-28-upstream-and-literature.md). The local runtime
baseline is unchanged since that review; its added commit contains research
notes. Unrelated uncommitted work was excluded. This pass changes documentation
only: no running services, robot behavior, dependencies or network settings.

Paper versions were checked against arXiv metadata; methods, experiments and
limitations were read from the full text. All reported performance below belongs
to the authors. Nothing was reproduced on Annie or a physical robot this week.

## DimOS changes

Reviewed `dimensionalOS/dimos` at
`db3d0ca9f31d726bfea3a78e8a57662292372d50` (4 October, 00:51:09 UTC),
[27 commits ahead][dimos-compare] of the previous review's
`edd7c346b8158a406c915dfe922202dc2d652e21`. This is a focused audit of the
following implementations and tests, not an exhaustive review of every change.
The repository [declares Apache-2.0][dimos-license]. Reuse should retain applicable
notices; no upstream code was imported or executed.

**Reject orphaned simulator state** ([4 October commit][dimos-shm-commit]).
The [MuJoCo shared-memory change][dimos-shm]
requires an advancing joint-state sequence when attaching, and records the
writer's host-monotonic timestamp. Adapters reject commands after state stops
updating; [tests][dimos-shm-tests] cover orphaned buffers, replacement by a fresh
simulator and stale-state rejection. The default stale interval is five seconds,
not an Annie operating requirement. This patch concerns manipulator/G1 adapters;
Annie's direct single-process Go1 viewer does not use that transport. Its useful
test principle is that a readable buffer or responsive endpoint does not prove
the producer is alive. Before any future SDK integration, test frozen state,
simulator restart and cancellation without reporting command acceptance as
executed motion.

**Bound obsolete-cloud waiting** ([1 October commit][dimos-ray-commit]).
The [ray-mapping change][dimos-ray] immediately
checks the transform cache when the newest transform has already advanced beyond
the cloud's timestamp tolerance, instead of waiting for a transform unlikely to
arrive. An optional `max_cloud_age_s` discards clouds relative to the latest
transform timestamp, preserving replay semantics; **the default is zero, so
age-based dropping is disabled**. Included Rust tests check the timestamp
predicates, not end-to-end planner recovery. For Annie's proposed scan-derived
mapper, add backlog, missing-transform and out-of-order replay cases before
adopting the pattern. Existing authored-map navigation does not acquire SLAM
from this change.

**Keep event time separate from arrival time** ([2 October commit][dimos-json-commit]).
The [JSON recorder change][dimos-json]
stores UTF-8 JSON and permits an explicit source-timestamp field. Without that
configuration it uses reception time. Missing, nonnumeric and nonfinite selected
timestamps fail; MCAP additionally rejects negative or unrepresentable times
instead of silently clamping. [Rust tests][dimos-json-tests] check source 12.5 s
and reception 13 s survive SQLite/MCAP storage, while [codec tests][dimos-json-codec]
cover valid text and malformed JSON. This is a concrete addition to last week's
replay proposal: record both clocks and explicit units. Annie camera-capture `ts`
uses milliseconds; the recorder interface uses seconds. Test that delayed old
evidence stays old after round-trip serialization. Adopting the full native
recorder is unnecessary for an initial synthetic fixture.

## G2-Nav: useful social costmaps, with an important execution caveat

Yuwen Liao et al., **G2-Nav: Grounded and Guarded Vision-Language Costmaps for
Robot Social Navigation**, [arXiv:2607.16956v2][g2-paper], revised 1 October 2026
(first submitted 18 July). Read §§3–4, §6 and latency Appendix C.
Paper distribution uses the arXiv perpetual non-exclusive license.

The method separates raw occupancy, goal attraction, traversability and social
costs. Visual reasoning assigns object scores asynchronously; new objects receive
a default score before a model response. A reflex cost penalizes unassigned
LiDAR points in a predicted motion corridor. This is a useful architectural
reference for social navigation, rather than a formal collision guarantee.

Authors report 83 recorded SCAND trials, and a separate physical Go2-W corridor
experiment with six trials per method. The latter uses a cloud VLM averaging
4 seconds per query and reports zero supervisor interventions for G2-Nav, but
still **6.9 seconds average within 0.3 m of obstacles**. Supervisor pause time
is excluded from travel-time and proximity metrics. These are bounded outdoor
hardware results, not Annie simulation results or evidence for indoor Go2 safety.

**Code evidence:** reviewed `centiLinda/G2-Nav` at
`df29d390b9c2d1ef4c4a7fde156478de34d9305e` (1 October), including its
[MIT license][g2-license], README and costmap implementation.
The release targets ROS1/SCAND; the paper's live robot used ROS2.
The [costmap callback][g2-code] is registered on an approximate synchronizer
requiring LiDAR, odometry, tracked objects and floor masks. Its reflex computation
is [inside that same callback][g2-reflex]. Therefore the released guard is not
independently driven by raw LiDAR when a required perception stream stops. Scores are retained
in a dictionary without an expiry in the inspected callback. Importing this node
would require explicit freshness and controller-timeout decisions.

**Annie opportunity:** extend delay/dropout evaluation around existing
[PersonSafety](../person_safety.py) and [per-tick control](../viewer.py), which
already have a one-second camera-freshness check and an authored resident
proximity guard under the stop policy. Introduce a crossing person while model
responses are delayed. In a candidate costmap adapter, separately stop
tracked-object and mask streams.
Measure capture-to-inhibition latency, minimum clearance and false completion.
Keep authored proximity protection separate from measured camera/LiDAR behavior.
If social costs are later added, semantic preferences must not clear occupied
cells or bypass the existing stop latch. No controller or threshold was changed.

## GlassGuard: evaluate missing obstacles and false blockage together

Hanwen Guo et al., **GlassGuard: Verified Glass Plane Mapping for Robot
Navigation**, [arXiv:2610.02110v1][glass-paper], 1 October 2026. Read §§III–V and
the conclusion. Paper distribution uses the arXiv perpetual non-exclusive license.

GlassGuard combines glass masks, LiDAR structural supports and image-derived
orientation checks. Global hypotheses can be merged or removed when later floor
or multi-view evidence contradicts them. The orientation check does **not**
independently verify metric depth. Multi-view removal trades fewer accumulated
false obstacles against gaps in current obstacle coverage.

The evaluation replays recordings from a physical wheeled robot: nine scenes,
six environments and 2.1 km. It is not a quantitative closed-loop navigation
success study. At **1 m voxel resolution**, the matched-pinhole comparison reports
82.1% ever-detected glass coverage and 16.7 false occupied voxels per frame,
versus GlassRecon's 44.4% and 85.4 (Table III). GG-pin's current coverage within
0–2 m is only 84.4% at 1 m resolution and 70.1% at 0.5 m (Table IV). Ever-detected
coverage must not be substituted for the map actually protecting the next step.
Near-vertical planar glass, segmentation, calibration and compute remain limits;
the reported 0.74 s/frame uses an RTX 5070 Ti laptop GPU.

**Code evidence:** the [released repository][glass-repo] was inspected at
`f5303bf7e8f7bb15812aee015eb9b62b960d9fca` (4 October): README,
the [angle gate][glass-angle] and hypothesis-pruning sections of `glassguard_core.py`,
launcher settings, and the [occupancy evaluator][glass-eval]. The evaluator exposes
field-of-view, coverage-dilation, accumulated-map and spill-grace options; these must be fixed
and reported in a comparison. No repository license file was present in the
recursive tree, and GitHub's license endpoint returned 404. **Source is readable;
permission to copy it into Annie is not established.** No source was imported.

**Annie opportunity:** [SpatialSensor](../spatial.py) uses geometric MuJoCo rays
without a material-dependent LiDAR transmission/reflection model. In a separate
future fixture, suppress or pass through selected glass returns while retaining
authored collision geometry as an evaluation oracle. Include a wrongly placed
glass hypothesis that blocks an open corridor. Report current near-field
coverage, false free cells, false occupied cells and recovery duration at an
Annie-relevant resolution, rather than importing the paper's 1 m grid into the
0.12 m planner. This is a proposed sensor-fault experiment, not a claim that the
current simulator models glass optics.

## Memory specificity: test whether the retrieved evidence matters

Aditi Tiwari et al., **Does Video Memory Use What It Retrieves? A Causal Audit
of Memory Specificity**, [arXiv:2609.12090v1][memory-paper], 10 September 2026.
This is newly reviewed here, not a new paper this week. Paper license: CC BY 4.0.
Read §§3–7; no implementation repository or code license was established from
the checked paper links.

The audit replaces the content consumed at one memory read while preserving its
interface and earlier computation. It compares correct, plausible wrong,
identity-free and content-free memories. In the studied DINO-WM setup,
training-memory means recover essentially the whole benefit on Ego-Exo4D and
7-Scenes: memory-on improvement alone does not establish episodic recall.
Conversely, SAM 2's DAVIS score falls from **0.926 to 0.182** when spatial memory
is replaced with another object's memory (ten sequences, Table 2). The effect
depends on architecture and task; these results do not establish that Annie
ignores its evidence. The intervention changes one read, and later recovery
can use intact memory. Latent-consistency scores are not robot task success.

**Annie opportunity:** extend existing [context tests](../../robot_backend/tests/test_context.py)
and [retrieval tests](../../app_backend/tests/test_semantic_memory.py) beyond
citation preservation and ranking. Use synthetic paired phone histories with
matched length/age/format but different valid locations. Hold the question,
current image, model and sampling settings fixed. Compare correct history,
plausible irrelevant history, contradicted history and no history. Require the
answer and cited frame to follow supporting evidence, or abstain when it is
absent. Retain provenance for each donor; use fresh synthetic IDs rather than
rewriting stored evidence under an existing frame ID. Cross-map/future evidence
should continue to be rejected before model calls. Mock-provider checks verify
the harness only; model grounding would require a separately recorded inference
evaluation. This adds a different test from last week's memory-selection study.

## Recommended next work

1. Extend offline delay/dropout tests, retaining separate source/reception times,
   to verify the guard and controller keep responding when perception stops.
2. Add paired-memory evaluation before investing in a more complex memory model.
3. Add transparent-obstacle and false-blockage fixtures when evaluating
   scan-derived mapping; retain the authored-map baseline separately.

Recommendations remain proposals. No SPEC/TODO requirement was changed. Source
inspection is not a test pass, and no runtime suites or physical experiments
were run for this documentation-only review.

[g2-paper]: https://arxiv.org/html/2607.16956v2
[g2-license]: https://github.com/centiLinda/G2-Nav/blob/df29d390b9c2d1ef4c4a7fde156478de34d9305e/LICENSE
[g2-code]: https://github.com/centiLinda/G2-Nav/blob/df29d390b9c2d1ef4c4a7fde156478de34d9305e/g2nav_ws/src/g2nav/scripts/costmap.py#L108-L116
[g2-reflex]: https://github.com/centiLinda/G2-Nav/blob/df29d390b9c2d1ef4c4a7fde156478de34d9305e/g2nav_ws/src/g2nav/scripts/costmap.py#L459-L515
[glass-paper]: https://arxiv.org/html/2610.02110v1
[glass-repo]: https://github.com/glassguardproject/GlassGuard/tree/f5303bf7e8f7bb15812aee015eb9b62b960d9fca
[glass-angle]: https://github.com/glassguardproject/GlassGuard/blob/f5303bf7e8f7bb15812aee015eb9b62b960d9fca/glassguard_core.py#L10134-L10206
[glass-eval]: https://github.com/glassguardproject/GlassGuard/blob/f5303bf7e8f7bb15812aee015eb9b62b960d9fca/tools/eval_occupancy.py#L113-L156
[memory-paper]: https://arxiv.org/html/2609.12090v1
[dimos-compare]: https://github.com/dimensionalOS/dimos/compare/edd7c346b8158a406c915dfe922202dc2d652e21...db3d0ca9f31d726bfea3a78e8a57662292372d50
[dimos-license]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/LICENSE
[dimos-shm-commit]: https://github.com/dimensionalOS/dimos/commit/db3d0ca9f31d726bfea3a78e8a57662292372d50
[dimos-ray-commit]: https://github.com/dimensionalOS/dimos/commit/239b4a8684b2f806f15aaec888f157ffe4d43d52
[dimos-json-commit]: https://github.com/dimensionalOS/dimos/commit/943ce13c67e679e75452557f9457f1ba58541091
[dimos-shm]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/dimos/simulation/engines/mujoco_shm.py#L394-L427
[dimos-shm-tests]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/dimos/hardware/manipulators/sim/test_shm_adapter.py
[dimos-ray]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/dimos/mapping/ray_tracing/rust/src/module.rs#L65-L118
[dimos-json]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/dimos/experimental/memory/rust/src/decoding.rs
[dimos-json-tests]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/dimos/experimental/memory/rust/src/lib.rs
[dimos-json-codec]: https://github.com/dimensionalOS/dimos/blob/db3d0ca9f31d726bfea3a78e8a57662292372d50/dimos/memory/codecs/test_json.py
