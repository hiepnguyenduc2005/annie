# Intelligent dog brain: design

Date: 2026-09-19. Status: proposed, awaiting operator approval.
Owner: robot brain (Henry, with Codex on the simulator/hardware tasks).

## Problem

The current brain is a reactive caption-to-command loop: one VLM call per tick
produces a caption and one action; memory retrieval is lexical and always asks
"where was the person last seen"; there is no representation of what the dog has
already searched; listening is replay-only; and the local planner takes 7-10 s, so
the 5 s freshness gate kills every local plan and the demo runs cloud-only
(`docs/LIVE_DEMO.md`, `robot/simulation/AI_BRAIN.md`, Codex review 2026-09-19).
The dog therefore revisits rooms, cannot re-plan on a sighting mid-route, and
decides "person on the floor" from a single 320 px image.

Goal: a dog that searches a home systematically, notices a person on the floor from
two independent signals, walks closer to check, asks, listens to a live reply, and
escalates, with every stage local-first and every decision cited to a frame.

## Non-goals

- No learned end-to-end policy (NaVILA, GR00T, pi0): no Go2 home-care checkpoint
  exists, and training is out of scope for the hackathon.
- No SLAM or Nav2 migration: waypoints and the existing navigation stay.
- No change to incident ownership, receipts, the 5 s gate, cloud budgets, or the
  two-frame evidence rule (`docs/ACCEPTANCE.md`, `robot/contract/README.md`).
- No raw DimOS movement authority. DimOS designs are borrowed; its executor is not.

## Approaches considered

| | Approach | Verdict |
| --- | --- | --- |
| A | Continuous perception + mission state + event-driven planner inside the existing services | **Recommended.** Fits contracts, testable offline, one day of work, visibly smarter. |
| B | Adopt the DimOS agentic blueprint (LangGraph + MCP skills, spatial memory) wholesale | Later. Apache-2.0 and maintained, but macOS support is alpha, the full SDK launch is unverified here (`docs/SIMULATION_FINDINGS.md`), and its skills own movement, which conflicts with Annie's receipt gates. Borrow its spatial-memory and skill designs. |
| C | Learned VLA / navigation policy | Not in scope (see non-goals). |

## Architecture (approach A)

Five components. New code lives under `robot/simulation/brain/` (pure policy
modules) and `robot/robot_backend/app/brain/` (service), honoring the existing
contracts in `robot/contract/brain.md` and `robot/contract/README.md`.

### 1. Perception loop, decoupled from planning

- YOLO11 person detection becomes tracking (Ultralytics `track`, ByteTrack, AGPL as
  already used) at ~5 Hz, giving persistent anonymous `track_id`s.
- A posture signal independent of the VLM: YOLO11n-pose keypoints, classified by a
  pure function into `upright | lying | unknown` from torso angle and hip/shoulder
  height relative to the box. Literature: OpenEQA shows VLMs are near-blind on
  spatial questions, and the staged fall-detection framework (arXiv:2507.10474)
  confirms falls with a detector, not a caption.
- The VLM (`/infer`, unchanged contract) runs on a bounded latest-frame queue at
  ~1 Hz; a slow answer is dropped, never re-dated (`docs/ARCHITECTURE.md` §7).
- Every perception publishes `brain.perception` immediately. Planning never blocks
  perception; a planner failure cannot discard an observation.
- The person-on-floor trigger requires two frames on the same track with
  `lying` from keypoints AND a floor-context VLM observation, or escalates to
  `unknown` which the planner resolves by moving closer and looking again.

### 2. Mission state (pure, deterministic, tested)

`robot/simulation/brain/mission.py`: `MissionState` with per-waypoint status
`unvisited | visited | inspected_empty | person_seen`, last sighting
`{track_id, frame_id, ts, pose, waypoint_id}`, current destination and command id,
recent outcomes, and a search order. Updated only from receipts and perception
events. Serialized into the planner context as a `mission` block via
`context.pack_context`. Provides `next_search_target()` so the search is
systematic even when the model is slow. This is the "explicit search progress"
both the Codex review and Inner Monologue-style planning call for.

### 3. Event-driven planner

- Re-plan on events (new sighting, terminal receipt, scene change, posture change,
  30 s timer), not every frame. Builds on the teammate's uncommitted
  `local_grounded_plan` split: image perception first, then a text-only action
  model (`qwen3:8b` or the `annie-qwen3-vl:2b` alias via Ollama JSON schema)
  choosing from `goto | look | say | wait | stop`, targeted at 300-800 ms so
  local plans survive the 5 s gate.
- Context gains `mission`, `current_perception` (tracks + posture), and
  goal-conditioned memories. `nearest_waypoint` is labelled as proximity, not
  location.
- Planning stays allowed during motion so the model can choose `stop` or `say`
  mid-route; `goto` while a command is open remains refused by the executor.

### 4. Semantic episodic memory

- Add an embedding column to the existing SQLite `memory` table using
  `nomic-embed-text` (Ollama, already pulled, Apache-2.0) and cosine retrieval;
  keep lexical retrieval as fallback. ReMEmbR-style: the query is the actual goal
  plus time, results stay cited `{caption, frame_id, ts, pose}`.
- Store negative evidence: `inspected_empty` entries per waypoint so memory can say
  "kitchen checked 40 s ago, nobody".

### 5. Conversation lifecycle

- A model `say` that ends in a question opens the same correlated 8 s listening
  window the incident policy uses.
- Additive contract migration: capture `source: microphone` beside
  `synthetic_replay`; live capture via the existing faster-whisper path with
  endpoint detection and playback exclusion. Never label microphone audio as
  replay.
- Intent classification moves from exact phrase lists to a small local LLM with
  `unknown` as a first-class outcome.

## Latency budget (warm, M1 Max, targets to measure)

| Stage | Target |
| --- | --- |
| Capture + YOLO track + pose, 5 Hz | 100-150 ms |
| Posture VLM on latest frame | 0.8-1.5 s |
| Memory retrieval | 10-50 ms |
| Text action selection | 300-800 ms |
| Gate + dispatch | < 50 ms |
| STT after endpoint | 200-600 ms |

Acceptance: two qualifying observations within 4 s of a staged fall; a local plan
accepted by the 5 s gate in 9 of 10 ticks; no revisit of an `inspected_empty`
waypoint while `unvisited` ones remain; a check-in question opens a listening
window every time.

## Open-source components

| Component | Use | License |
| --- | --- | --- |
| Ultralytics YOLO11 (detect, pose, ByteTrack) | tracking + posture | AGPL-3.0 (already in use) |
| Ollama + qwen3-vl:2b, qwen3:8b, nomic-embed-text | perception, action, embeddings | Apache-2.0 models |
| faster-whisper / whisper.cpp | STT | MIT |
| DimOS (dimensionalOS) | spatial-memory and skill design reference; blueprints for later | Apache-2.0 |
| ReMEmbR (NVIDIA) | retrieval design | Apache-2.0 |
| SmolVLM-500M, Moondream2 | candidate faster posture VLMs to benchmark against Qwen3-VL | Apache-2.0 |
| FastVLM (Apple) | not adopted: research-only weights | research license |

## Delivery order

1. Mission state + event-driven re-planning + perception decoupling (bridge,
   context, planner prompt). Biggest visible gain.
2. Posture signal and track persistence for the fall trigger.
3. Embedding memory and negative evidence.
4. Conversation lifecycle and live microphone.
5. Later: DimOS skills/spatial memory, SmolVLM/Moondream benchmark, hardware.

Each step ships with pure tests, mocked-HTTP bridge tests, and the documented
checks (`docs/ACCEPTANCE.md` §14). The teammate's uncommitted planner/bridge work
is built on, not replaced, and is committed only with their agreement.

## Survey results (2026-09-19, seven specialist searches, verified live)

These change the plan in four places. Full per-source notes are in the
session transcript; the decisive items are listed here with links.

1. **Vision latency is the runtime, not the model.** The recorded Qwen3-VL 2B
   best case is 0.62 s on this M1 Max, so the 3.8 s mean is Ollama overhead
   (open Qwen3-VL slowness issues: ollama/ollama#12854, #12882, #14548). Path to
   sub-second: `mlx-vlm` server with capped image tokens and a ~10-token JSON
   answer. Candidates to benchmark in order: LFM2.5-VL-450M (LFM Open License,
   242 ms at 512² on Jetson Orin), Qwen3.5-2B (Apache-2.0, thinking off),
   Gemma 4 E2B (Apache-2.0, 70-140 image tokens), Moondream 2. FastVLM has the
   best published TTFT (166 ms on an M1) but its weights are research-only, so
   it is demo-only and must be recorded for compliance.
2. **Fall decision.** No published Go2 fall-response work exists. Zero-shot
   8B VLMs score F1 ≈ 0.3 on the *fallen state* (OmniFall, arXiv 2505.19889),
   so the VLM cannot tell "fell" from "lying down"; the signals that can are
   location (floor vs bed/sofa from the map) and a verbal check-in. Omobot
   (arXiv 2408.05315) measured that COCO-pose accuracy drops at a 0.15 m camera
   and hand-written rules beat an MLP: our posture rule is the right shape, and
   the Go2's ~0.4 m camera needs testing on its own footage. E-FPDS
   (gram.web.uah.es/data/datasets/fpds) is the only robot-viewpoint fallen-person
   dataset; use it for evaluation and, later, fine-tuning. Responsiveness is
   reported on the ACVPU scale as answered / unclear / no response after N
   prompts, never "unconscious".
3. **Speech stack.** silero-vad → Moonshine v2 Small (MIT, 148 ms on an M3)
   A/B parakeet-mlx → rule-based intent → Kokoro-82M (Apache-2.0) with fixed
   lines pre-rendered, macOS `say` as fallback; ~0.75-0.95 s end-of-speech to
   reply. Whisper hallucinates on silence (arXiv 2505.12969), so STT is gated
   by VAD with a minimum speech duration and an empty reply escalates.
   faster-whisper is CPU-only on a Mac; fine on the GX10.
4. **Memory.** Keep the sqlite caption/pose/time table (it is ReMEmbR's schema
   minus embeddings); add a text embedding and, later, a MobileCLIP image
   embedding per keyframe via sqlite-vec. All open-vocabulary 3D mappers need
   posed RGB-D and CUDA; skip for the hackathon, evaluate DualMap on the GX10.
   ReMEmbR's code is NVIDIA non-commercial: copy the design only.

Confirmed choices: DimOS + `unitree_webrtc_connect` over WebRTC is the only
Go2 path without ROS 2, Ubuntu or an EDU unit; only one WebRTC client can be
connected at a time (an open phone app yields `RobotBusyError`); general VLAs
(OpenVLA, GR00T, π0) emit arm actions and do not apply; navigation VLAs
(NaVILA, InternVLA-N1, OmniVLA) are GX10-later work. The planner loop stays a
ReAct-style tool loop with typed actions and Inner-Monologue feedback; KnowNo's
multiple-choice shape ({fallen, resting, sitting, no person, unsure}) with
escalation on anything but a confident single answer is adopted for the
incident question.
