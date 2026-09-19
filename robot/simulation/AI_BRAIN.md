# Image-grounded action planner

Annie's default autonomous demo uses a vision-language model to choose its
next action. The user supplies a goal. Each turn receives a real rendered
robot-camera JPEG, capture pose/time, the named navigable waypoints, up to
six image-derived memories, and recent command execution or rejection
feedback. Authored grandma activity and scenario labels never enter the
planner request.

One model call returns both a perception observation and one action:
`goto`, `look`, `say`, `wait`, or `stop`. The model supplies its brief reason.
Perception enters the existing incident policy; the proposed action passes
through `agent_execution.py` before entering the app command queue.
Completion still requires measured motion or actual playback receipts.

The execution gate does not choose destinations. It rejects expired or
cross-scene decisions, unknown waypoints, commands that interrupt active
motion, and movement blocked by person/camera/physics interlocks. A pending
check-in owns speech and motion. Speech is rate-limited to avoid overlapping
or repeated chatter. Refusals are fed back on the next model turn.

`autonomy.py` remains a deterministic diagnostic coordinator for comparison;
it is not the default model brain. `--perception agent` selects the actual
model planner. No rule-based or paid-provider fallback silently replaces it.

```sh
# Reuse the installed public Qwen weights with a bounded local context.
.venv/bin/python robot/simulation/prepare_local_brain.py
.venv/bin/python robot/simulation/run_brain.py \
  --mode local --model annie-qwen3-vl:2b --port 8004
.venv/bin/python -m robot.simulation.bridge --perception agent \
  --brain-url http://127.0.0.1:8004 --continuous-local \
  --inference-interval .35 --poll-interval .1
```

In the viewer, enter a goal and select **Start AI / resume**. **Pause AI**
stops motion and rejects any in-flight decision after it returns. The panel
shows the model, action, reason, latency and execution disposition.

Continuous mode verifies that the brain is local before removing the call
allowance. It waits for terminal command receipts before planning again and
holds during incident playback/reply windows. Original capture timestamps are
never refreshed: plans older than five seconds are still rejected. After a
risky image, a short `/infer` call confirms posture using a new real camera
frame instead of generating another route plan.

Local Qwen uses an 8,192-token context alias and schema-constrained JSON.
The original 262,144-token runtime allocation consumed about 32.9 GB; the
alias consumed about 2.56 GB. Measurements are host-specific. A smaller
context improves resource use but does not establish perception accuracy.
See [Ollama context configuration](https://docs.ollama.com/context-length)
and [structured outputs](https://docs.ollama.com/capabilities/structured-outputs).

Before each fresh capture, the planner retrieves cited, same-map person
observations from the app's durable SQLite memory. When configured with
`ANNIE_MEMORY_PROVIDER=elastic`, it uses the Elastic adapter instead and
persists accepted synthetic captions in a background task. Retrieval/write
failures are visible; unavailable Elastic is not relabeled as connected.
No Elastic, Zep, or Token Company credentials were present during the local
acceptance work. The working context remains bounded to 12 KB and six
complete citations; cloud output is capped at 512 tokens, local at 256.

For the staged full-house acceptance run, stop the ordinary body bridge and
press **Run full-house demo** in the viewer. The driver owns the same exclusive
delivery lease, stages the environment and recorded reply, and lets the
model choose robot actions. The live report is `/demo-state`; completed run
records are archived under ignored `output/house-demo-<run_id>.json`.

Cloud calls accept synthetic rendered frames only. Both `/infer` and `/plan`
share the same attempt-reservation ledger and provider lock. The existing
$20 session cap includes earlier vision/audio experiments and the separate
$1 probe reservation; the service cap is set to $19 to preserve that reserve.
A bounded run ends when its call allowance is used; motion commands already
in flight can finish, and command receipts continue to be processed.

This is model-directed planning over a known map, not learned locomotion,
SLAM, calibrated person localization, validated clinical reasoning, or Go2
hardware deployment. Human daily-life animation is authored separately.
