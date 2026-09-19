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
# Run the existing brain with the explicitly configured, approved model.
ANNIE_VISION_BUDGET_USD=19 .venv/bin/python robot/simulation/run_brain.py \
  --mode cloud --model google/gemini-2.5-flash-lite:floor --port 8003
.venv/bin/python -m robot.simulation.bridge --perception agent \
  --brain-url http://127.0.0.1:8003 --max-inferences 20 \
  --inference-interval 3 --poll-interval .25
```

In the viewer, enter a goal and select **Start AI / resume**. **Pause AI**
stops motion and rejects any in-flight decision after it returns. The panel
shows the model, action, reason, latency and execution disposition.

Cloud calls accept synthetic rendered frames only. Both `/infer` and `/plan`
share the same attempt-reservation ledger and provider lock. The existing
$20 session cap includes earlier vision/audio experiments and the separate
$1 probe reservation; the service cap is set to $19 to preserve that reserve.
A bounded run ends when its call allowance is used; motion commands already
in flight can finish, and command receipts continue to be processed.

This is model-directed planning over a known map, not learned locomotion,
SLAM, calibrated person localization, validated clinical reasoning, or Go2
hardware deployment. Human daily-life animation is authored separately.
