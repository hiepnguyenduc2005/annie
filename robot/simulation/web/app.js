"use strict";
const $ = (id) => document.getElementById(id);
let current = null;
let busy = false;
let rendered = 0;
let rateStart = performance.now();
let wasConnected = false;
let catalog = { scenes: [] };
let catalogGeneration = null;
let catalogRequest = false;
let generationPending = false;
let activeSceneId = null;
let intelligenceRevision = null;
let demoStarting = false;
let demoStagesSignature = null;
let previousDemoStatus = null;
const pendingOverlays = new Map();

async function loadCatalog() {
  if (catalogRequest) return;
  catalogRequest = true;
  try {
    const response = await fetch("/scenes", { cache: "no-store" });
    if (!response.ok) throw new Error("Scene factory is starting");
    catalog = await response.json();
    const select = $("scene-select");
    select.replaceChildren();
    for (const scene of catalog.scenes) {
      const option = document.createElement("option");
      option.value = scene.id;
      option.textContent = scene.title;
      select.append(option);
    }
    $("scene-count").textContent = `${catalog.scenes.length} SCENES`;
    if (catalog.seed != null) $("factory-seed").value = catalog.seed;
    renderScene();
  } catch (error) {
    $("scene-count").textContent = "Catalog unavailable";
  } finally {
    catalogRequest = false;
  }
}

function renderMission(currentState) {
  const available =
    !!currentState.locomotion &&
    !currentState.physics_error &&
    !generationPending;
  const loco = currentState.locomotion ?? {};
  const nav = currentState.navigation ?? null;
  for (const id of [
    "mission-patrol",
    "mission-goto-bedroom",
    "mission-goto-living-room",
    "mission-come-home",
    "mission-turn-left",
    "mission-look",
    "mission-stop",
    "mission-resume",
  ])
    $(id).disabled = !available || busy;
  if (!available) {
    $("mission-status").textContent = currentState.physics_error
      ? "Missions FAILED: physics error, reset the simulation."
      : "Missions unavailable: locomotion controller not loaded.";
    return;
  }
  const base = currentState.qpos_base ?? [];
  const position =
    base.length >= 2
      ? `Base at x ${Number(base[0]).toFixed(2)} m, y ${Number(base[1]).toFixed(2)} m`
      : "Base position unavailable";
  if (nav?.active_command?.status === "failed" || nav?.active_command?.status === "blocked") {
    $("mission-status").textContent = `Mission ${nav.active_command.status}: ${nav.active_command.detail ?? "command not completed"} · ${position} · ${loco.label ?? "Go1 locomotion surrogate"}`;
    return;
  }
  const detail = nav?.active_command?.detail
    ? `${nav.active_command.status}: ${nav.active_command.detail}`
    : nav?.state
      ? `Navigation ${nav.state}`
      : "No mission";
  const target = nav?.waypoint ? ` toward ${nav.waypoint}` : "";
  $("mission-status").textContent =
    `${detail}${target} · ${position} · ${loco.label ?? "Go1 locomotion surrogate"}`;
}

function currentYaw() {
  const quat = current?.qpos_base;
  if (Array.isArray(quat) && quat.length >= 7) {
    const [w, x, y, z] = quat.slice(3, 7);
    const yaw = Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
    if (Number.isFinite(yaw)) return yaw;
  }
  return null;
}

function normalizeAngle(value) {
  return Math.atan2(Math.sin(value), Math.cos(value));
}

function turnLeftQuarter() {
  const yaw = currentYaw();
  if (yaw === null) {
    notice("Cannot read measured yaw yet - try again once telemetry is live.", true);
    return;
  }
  const heading = normalizeAngle(yaw + Math.PI / 2);
  control({ action: "mission", cmd: "turn", heading });
}

function mission(cmd, waypoint) {
  const payload = { action: "mission", cmd };
  if (waypoint) payload.waypoint = waypoint;
  control(payload);
}

function renderScene() {
  const scene = catalog.scenes.find((item) => item.id === current?.scene_id);
  if (scene) {
    $("scene-select").value = scene.id;
    $("scene-label").textContent = scene.title;
    $("scene-description").textContent = scene.description;
    $("ground-truth").textContent = JSON.stringify(scene.ground_truth, null, 2);
    if (activeSceneId !== scene.id) {
      activeSceneId = scene.id;
      document.querySelectorAll('[data-camera]').forEach(button => button.setAttribute('aria-pressed', 'false'));
      notice(
        `Loaded: ${scene.title}. Simulation ${current?.running ? "running" : "paused at its starting pose"}.`,
      );
    }
  }
  for (const id of ["previous-scene", "next-scene", "scene-select"])
    $(id).disabled = catalog.scenes.length === 0;
}

function adjacentScene(offset) {
  if (!catalog.scenes.length) return;
  const index = Math.max(
    0,
    catalog.scenes.findIndex((scene) => scene.id === current?.scene_id),
  );
  const scene =
    catalog.scenes[
      (index + offset + catalog.scenes.length) % catalog.scenes.length
    ];
  control({ action: "scene", id: scene.id });
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
}

async function control(payload) {
  if (busy) return;
  busy = true;
  try {
    const response = await fetch("/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`Control rejected (${response.status})`);
    if (payload.action === "generate") generationPending = true;
    notice(
      payload.action === "generate"
        ? "Generating a repeatable batch of home scenes…"
        : payload.action === "scene"
          ? "Loading scene…"
          : `${payload.action === "camera" ? "Camera change" : "Simulation control"} accepted.`,
    );
    await pollState();
    return true;
  } catch (error) {
    notice(error.message, true);
    return false;
  } finally {
    busy = false;
  }
}

async function pollState() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw new Error("Simulator unavailable");
    current = await response.json();
    cameraController.sync(current);
    renderSpatial(current);
    if (!wasConnected && catalogGeneration !== null) catalogGeneration = null;
    const generation = JSON.stringify(current.catalog_generation ?? null);
    if (generation !== catalogGeneration) {
      catalogGeneration = generation;
      await loadCatalog();
      if (generationPending)
        notice(`Batch ready: ${catalog.scenes.length} repeatable scenes.`);
      generationPending = false;
    }
    renderScene();
    if (!wasConnected && current.ready) {
      notice(
        "Renderer ready. Play the simulation or advance one physics step.",
      );
      wasConnected = true;
    }
    renderMission(current);
    if (intelligenceRevision !== current.intelligence_revision) {
      intelligenceRevision=current.intelligence_revision;
      if (document.activeElement !== $('intelligence-goal') && current.intelligence_goal)
        $('intelligence-goal').value=current.intelligence_goal;
    }
    $('demo-voice').closest('label').hidden = current.audio_output === 'native';
    $('browser-voice-hint').hidden = current.audio_output === 'native';
    const resident = current.resident;
    $('resident-activity').textContent = resident?.activity || 'Resident routine';
    $('resident-detail').textContent = resident
      ? `${resident.posture || resident.phase || 'Daily routine'} · ${Number(current.simulation_time).toFixed(1)} s into the scene`
      : 'Select an animated daily-life scene. Fixed-pose test scenes remain available.';
    document.querySelectorAll('[data-resident]').forEach(button => { button.disabled = !resident || busy; });
    $('stairs-capability').textContent = current.current_scene?.stairs
      ? 'Two physical floors · 18 household steps · upstairs walking unavailable with the current policy.' : '';
    $("connection").textContent = "Simulator connected";
    $("connection-dot").className = "online";
    $("run-state").textContent = current.physics_error
      ? "Reset required"
      : current.running
        ? "Running"
        : "Paused";
    $("play").textContent = current.running
      ? "Pause simulation"
      : "Play simulation";
    $("reset").disabled = false;
    for (const id of ["play", "step"]) $(id).disabled = !!current.physics_error;
    $("sim-time").textContent =
      current.simulation_time == null
        ? "—"
        : `${Number(current.simulation_time).toFixed(3)} s`;
    $("steps").textContent = Number(current.steps ?? 0).toLocaleString();
    const base = current.base_position ?? current.base_pos ?? current.qpos_base;
    const height = current.base_z ?? (Array.isArray(base) ? base[2] : null);
    $("base-height").textContent =
      height == null ? "—" : `${Number(height).toFixed(3)} m`;
    $("timestep").textContent =
      `${(Number(current.timestep ?? current.physics_dt ?? 0) * 1000).toFixed(1)} ms`;
    $("wall-time").textContent =
      current.active_wall_time == null
        ? "—"
        : `${current.active_wall_time.toFixed(1)} s`;
    $("achieved-speed").textContent =
      current.achieved_speed == null
        ? "Paused"
        : `${current.achieved_speed.toFixed(2)}×`;
    $("day-time").textContent = current.time_of_day ?? "09:00:00";
    if (document.activeElement !== $("speed"))
      $("speed").value = String(current.requested_speed ?? 1);
    $("phase").textContent = current.current_phase
      ? `Scheduled phase: ${current.current_phase}`
      : current.resident ? `Routine: ${current.resident.activity} · ${Math.round(current.resident.cycle_duration_s)} s demo day` : "Fixed-pose test scene.";
    if (current.dropped_wall_seconds > 0.1)
      notice(
        `Rendering cannot keep up: ${current.dropped_wall_seconds.toFixed(2)} s of wall time skipped. Measured speed shows actual progress.`,
        true,
      );
    const modelLabel = /go1/i.test(current.modelname ?? "")
      ? "Unitree Go1 locomotion surrogate"
      : current.modelname ?? "Unitree Go2";
    $("version").textContent =
      `MuJoCo ${current.mujoco_version ?? ""} · ${modelLabel} · local rendering`;
    if (current.modelname) $("header-model").textContent = modelLabel;
    if (current.control_mode)
      $("control-mode").textContent = current.control_mode;
    if (typeof current.hold_enabled === "boolean")
      $("hold").checked = current.hold_enabled;
    if (current.render_error) notice(current.render_error, true);
    if (current.physics_error) notice(current.physics_error, true);
    if (current.catalog_error) notice(current.catalog_error, true);
    if (current.scene_error) notice(current.scene_error, true);
    if (current.catalog_error) generationPending = false;
    $("generate").disabled = generationPending || !!current.scene_loading;
    $("hold").disabled = !current.hold_supported;
    document.querySelector('[data-camera="room"]').disabled =
      !current.current_scene?.overview;
  } catch (error) {
    wasConnected = false;
    $("connection").textContent = "Simulator disconnected";
    $("connection-dot").className = "";
    for (const id of ["play", "step", "reset"]) $(id).disabled = true;
    notice(error.message, true);
  }
}

function nextFrame() {
  if (document.hidden) {
    setTimeout(nextFrame, 500);
    return;
  }
  const image = new Image();
  image.onload = () => {
    $("frame").src = image.src;
    $("frame").className = "ready";
    $("loading").hidden = true;
    rendered += 1;
    const elapsed = performance.now() - rateStart;
    if (elapsed > 2000) {
      $("render-rate").textContent =
        `${((rendered / elapsed) * 1000).toFixed(1)} frames/s received`;
      rendered = 0;
      rateStart = performance.now();
    }
    setTimeout(nextFrame, 100);
  };
  image.onerror = () => setTimeout(nextFrame, 1000);
  image.src = `/frame.jpg?t=${Date.now()}`;
}

$("play").addEventListener("click", () =>
  control({ action: current?.running ? "pause" : "play" }),
);
$("step").addEventListener("click", () => control({ action: "step" }));
$("reset").addEventListener("click", () => control({ action: "reset" }));
$("speed").addEventListener("change", () =>
  control({ action: "speed", value: Number($("speed").value) }),
);
$("hold").addEventListener("change", () =>
  control({ action: "hold", enabled: $("hold").checked }),
);
$("scene-select").addEventListener("change", () =>
  control({ action: "scene", id: $("scene-select").value }),
);
$("previous-scene").addEventListener("click", () => adjacentScene(-1));
$("next-scene").addEventListener("click", () => adjacentScene(1));
$("factory-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("generate").disabled = true;
  try {
    await control({
      action: "generate",
      count: Number($("factory-count").value),
      seed: Number($("factory-seed").value),
    });
  } finally {
    $("generate").disabled = generationPending;
  }
});
for (const button of document.querySelectorAll("[data-camera]")) {
  button.setAttribute("aria-pressed", "false");
  button.addEventListener("click", async () => {
    const accepted = await control({ action: "camera", preset: button.dataset.camera });
    if (accepted) document.querySelectorAll("[data-camera]").forEach(item => {
      item.setAttribute("aria-pressed", String(item === button));
    });
  });
}
document.querySelectorAll('[data-resident]').forEach(button => {
  button.addEventListener('click', () => control({action:'resident',cmd:button.dataset.resident}));
});
$('intelligence-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('intelligence-start');
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = 'Starting AI…';
  setPanelError('ai-error', null);
  try {
    await postJSON('/agent/start', {action:'intelligence', enabled:true, goal:$('intelligence-goal').value});
  } catch (error) {
    setPanelError('ai-error', error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Start AI / resume';
  }
});
$('intelligence-stop').addEventListener('click',()=>control({action:'intelligence',enabled:false,goal:$('intelligence-goal').value}));
$('open-house').addEventListener('click', async () => {
  if (!catalog.scenes.some(scene => scene.id === 'grandmas-house')) {
    notice('Full house is being loaded into the scene catalog.');
    return;
  }
  await control({action:'scene',id:'grandmas-house'});
  await control({action:'play'});
});
document.addEventListener("keydown", (event) => {
  if (
    event.code === "Space" && !event.repeat &&
    !event.target.closest('input, select, textarea, button, a, summary, [contenteditable], [role="tab"]')
  ) {
    event.preventDefault();
    if (current && !current.physics_error)
      control({ action: current.running ? "pause" : "play" });
  }
});
async function stateLoop() {
  await pollState();
  setTimeout(stateLoop, 500);
}

function nextDogFrame() {
  if (document.hidden) {
    setTimeout(nextDogFrame, 500);
    return;
  }
  const image = new Image();
  image.onload = () => {
    $("dog-frame").src = image.src;
    $("dog-frame").className = "ready";
    setTimeout(nextDogFrame, 300);
  };
  image.onerror = () => setTimeout(nextDogFrame, 1500);
  image.src = `/robot-frame.jpg?t=${Date.now()}`;
}

for (const [id, cmd, waypoint] of [
  ["mission-patrol", "patrol", null],
  ["mission-goto-bedroom", "goto", "bedroom"],
  ["mission-goto-living-room", "goto", "living-room"],
  ["mission-come-home", "goto", "home"],
  ["mission-look", "look", null],
  ["mission-stop", "stop", null],
  ["mission-resume", "resume", null],
])
  $(id).addEventListener("click", () => mission(cmd, waypoint));
  $("mission-turn-left").addEventListener("click", turnLeftQuarter);
stateLoop();
loadCatalog();
nextFrame();
nextDogFrame();

/* ---- Brain state, voice output, and demo transcription panels ---- */

function setPanelError(id, message) {
  const target = $(id);
  target.textContent = message || "";
  target.hidden = !message;
}

async function postJSON(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof data.error === "string"
        ? data.error
        : `Request rejected (${response.status})`,
    );
  }
  return data;
}

function renderPersonSafety(safety) {
  const badge = $("safety-state");
  const stateText = $("safety-state-text");
  const reason = $("safety-reason");
  const confidence = $("safety-confidence");
  const latency = $("safety-latency");
  const error = $("safety-error");
  if (!safety || typeof safety !== "object") {
    badge.textContent = "NO DATA";
    stateText.textContent = "Person safety not reported by this simulator build yet.";
    reason.textContent = "—";
    confidence.textContent = "—";
    latency.textContent = "—";
    error.hidden = true;
    return;
  }
  const advisory = safety.enforced === false;
  badge.textContent = !safety.enabled ? 'DISABLED' : !safety.ready ? 'WARMING' : advisory ? 'ADVISORY' : safety.blocked ? 'BLOCKED' : 'READY';
  stateText.textContent = !safety.enabled ? 'Camera detection is not enabled.' : !safety.ready ? 'Waiting for a fresh detector result.' : advisory ? 'Person detections inform the model; the model chooses the action.' : safety.blocked ? 'Movement inhibited.' : 'Fresh detector result; ready for a mission.';
  reason.textContent = safety.reason ?? "—";
  confidence.textContent =
    !safety.detections?.length
      ? "—"
      : `${(Math.max(...safety.detections.map(d => d.confidence)) * 100).toFixed(0)}% (model estimate)`;
  latency.textContent =
    safety.latency_ms == null ? "—" : `${Number(safety.latency_ms).toFixed(0)} ms`;
  if (safety.blocked && !advisory) {
    error.textContent = safety.reason ? `Stop reason: ${safety.reason}` : "Person stop active.";
    error.hidden = false;
  } else {
    error.hidden = true;
  }
}

async function pollBrain() {
  const fields = [
    "brain-caption",
    "brain-posture",
    "brain-location",
    "brain-confidence",
    "brain-frame",
    "brain-latency",
    "brain-provider",
    "brain-ingest",
    "brain-updated",
  ];
  try {
    const response = await fetch("/brain-state", { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(data.last_error || "Brain bridge not connected");
    const agent = data.agent;
    const completion = data.goal_completion;
    const completed = completion && completion.map_id === current?.map_id &&
      completion.revision === current?.intelligence_revision && completion.goal === current?.intelligence_goal;
    $('goal-completion').hidden = !completed;
    $('goal-completion').textContent = completed ? `Goal completed · ${completion.result}` : '';
    renderExploration(data);
    $('agent-memory').textContent = `Memory: ${data.memory?.provider || 'unavailable'} · ${data.memory?.retrieved ?? 0} retrieved citations · ${data.memory?.status || 'waiting'}`;
    $('agent-history').replaceChildren(...(data.history || []).slice(-4).map(item => {
      const row=document.createElement('li');
      const receipt=current?.navigation?.commands?.find(c=>c.command_id===item.command_id);
      row.textContent=`${item.action.action}${item.action.waypoint_id ? ' → '+item.action.waypoint_id : ''} · ${receipt?.status || item.execution} · ${Math.round(item.latency_ms)} ms`;
      return row;
    }));
    $('autonomy-status').textContent = agent
      ? `${completed ? 'Goal completed' : current?.intelligence_enabled ? (agent.thinking ? 'Thinking…' : agent.action?.action || 'Ready') : 'Paused'} · ${agent.model || 'vision-language model'}${agent.latency_ms ? ' · '+Math.round(agent.latency_ms)+' ms' : ''}`
      : 'Waiting for the model planner.';
    $('agent-reason').textContent = agent ? `${agent.action?.reason || ''} · ${agent.execution || ''}` : '';
    $('agent-context').textContent = agent?.context
      ? `${agent.context.memories_included} cited memories · ~${agent.context.estimated_text_tokens} context text tokens · ${agent.usage?.prompt_tokens ?? '—'} actual input tokens including image · ${agent.context.output_token_limit} output-token cap`
      : '';
    const mode =
      data.last_provider?.mode === "local"
        ? "LOCAL MODEL"
        : data.last_provider?.mode === "cloud"
          ? "CLOUD MODEL"
          : data.continuous_local ? 'LOCAL MODEL · CONTINUOUS' : data.perception_mode === 'agent' ? 'PLANNER STARTING' : data.perception_mode === 'vision' ? 'VISION' : data.perception_mode === 'ground-truth' ? 'AUTHORED LABELS' : "DISABLED";
    const sameMap = !data.last_perception || data.last_perception.pose?.map_id === current?.map_id;
    const p = sameMap ? data.last_perception ?? null : null;
    const stale = p && Date.now() - p.ts > 5000;
    $("brain-mode").textContent = data.inference_limit_reached ? 'CALL LIMIT REACHED' : stale ? 'STALE OBSERVATION' : mode;
    $("brain-caption").textContent = p?.caption
      ? String(p.caption).slice(0, 300)
      : p
        ? "No caption yet"
        : "No observation yet";
    $("brain-posture").textContent = p?.posture ?? "—";
    $("brain-location").textContent = p?.location ?? "—";
    $("brain-confidence").textContent =
      p?.confidence == null
        ? "—"
        : `${(Number(p.confidence) * 100).toFixed(0)}% (model estimate)`;
    $("brain-frame").textContent = p?.frame_id
      ? `…${String(p.frame_id).slice(-8)}`
      : "—";
    $("brain-latency").textContent =
      data.last_latency_ms == null
        ? "—"
        : `${Number(data.last_latency_ms).toFixed(0)} ms`;
    const provider = data.last_provider ?? {};
    const usage = provider.usage?.total_tokens;
    $("brain-provider").textContent = provider.model
      ? usage == null
        ? provider.model
        : `${provider.model} · ${Number(usage).toLocaleString()} tokens`
      : mode === "DISABLED"
        ? "Not configured"
        : "—";
    $("brain-ingest").textContent =
      data.ingest_accepted == null
        ? "—"
        : data.ingest_accepted
          ? "Accepted"
          : "Rejected";
    $("brain-updated").textContent = p?.ts
      ? new Date(p.ts).toLocaleTimeString()
      : "—";
    $("brain-note").textContent =
      data.inference_limit_reached ? 'Configured inference allowance used; movement and voice remain available. Restart the bridge deliberately for a new bounded run.' : !sameMap ? 'Waiting for an observation from the current scene.' : stale ? 'Historical capture; this is not a current view of the resident.' : mode === "DISABLED"
        ? "Vision inference is disabled in the brain bridge configuration; this panel shows connection state only. Scene labels in the ground truth section are authored simulation data, not image inference."
        : "Actual image inference from the configured vision model — distinct from the authored simulation ground truth section.";
    renderPersonSafety(current?.person_safety);
    setPanelError("brain-error", data.last_error || null);
  } catch (error) {
    $("brain-mode").textContent = "DISCONNECTED";
    $('autonomy-status').textContent = 'Planner disconnected';
    $('planner-freshness').hidden = false;
    $('planner-freshness').textContent = 'Saved actions and room visits may be out of date.';
    renderPersonSafety(null);
    for (const id of fields) $(id).textContent = "—";
    setPanelError("brain-error", `Brain bridge: ${error.message}`);
  }
}

function wireSayClip(commandId, url, durationS, text, playback) {
  const audio = $("say-audio");
  const native = playback === "native";
  const nativeNote = $("say-native");
  nativeNote.hidden = !native;
  const meta =
    `Clip ${commandId.slice(0, 8)}` +
    (durationS == null ? "" : ` · ${Number(durationS).toFixed(1)} s`) +
    ` · ${url}`;
  $("say-audio-wrap").hidden = false;
  audio.hidden = native;
  $("say-meta").textContent = meta;
  $("say-clip-text").textContent = text
    ? `Says: ${String(text).slice(0, 500)}`
    : "";
  if (native) {
    audio.removeAttribute("src");
  } else {
    audio.src = url;
  }
  const playButton = $("say-play");
  if (native) {
    playButton.hidden = true;
    playButton.onclick = null;
    audio.onended = null;
    return;
  }
  playButton.hidden = false;
  playButton.onclick = () => audio.play().catch(() => {});
  audio.onended = () => {
    // Receipt only on real browser playback end — never synthesized.
    postJSON("/speech/played", { command_id: commandId })
      .then(() => {
        $("say-status").textContent = "Playback acknowledged by the backend.";
      })
      .catch((error) => {
        $("say-status").textContent = `Playback ack failed: ${error.message}`;
      });
  };
}

let lastSeenClipId = null;
let activeNativeId = null;

function renderNativeStatus(clip) {
  if (clip?.playback !== "native") return null;
  const output = clip.output ?? "macos_default_output";
  let label;
  if (clip.status === "played") {
    label = "Native playback finished (playback_process_completed).";
  } else if (clip.status === "failed") {
    label = "Native playback FAILED: " + (clip.playback_error ?? "unknown error") + ".";
  } else if (clip.status === "playing") {
    label = "Playing natively now...";
  } else {
    label = "Queued for native playback...";
  }
  return label + " | output: " + output + " | no browser replay for native clips.";
}

async function pollQueuedClips() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    const clips = Array.isArray(data?.speech) ? data.speech : [];
    if (!clips.length) return;
    const latest = clips[clips.length - 1];
    const nativeNote = $("say-native");
    if (activeNativeId) {
      const tracked = clips.find((clip) => clip.command_id === activeNativeId);
      if (tracked) nativeNote.textContent = renderNativeStatus(tracked);
    }
    if (
      !latest?.command_id ||
      latest.command_id === lastSeenClipId ||
      typeof latest.url !== "string" ||
      !latest.url.startsWith("/speech/")
    )
      return;
    lastSeenClipId = latest.command_id;
    wireSayClip(latest.command_id, latest.url, latest.duration_s, latest.text, latest.playback);
    if (latest.playback === "native") {
      activeNativeId = latest.command_id;
      nativeNote.textContent = renderNativeStatus(latest);
      $("say-status").textContent = "Native audio selected - clip plays on the computer default output, not in this tab.";
      return;
    }
    if ($("demo-voice").checked) {
      const audio = $("say-audio");
      try {
        await audio.play();
        $("say-status").textContent =
          "Demo voice enabled — playing the latest clip automatically.";
      } catch (error) {
        $("say-status").textContent =
          `Autoplay was blocked (${error.name || "play failed"}). Use Hear Annie — no played receipt was sent.`;
      }
    } else {
      $("say-status").textContent =
        "New synthesized clip ready — press Hear Annie to play it.";
    }
  } catch {
    // /state connection problems surface in the main simulator panel.
  }
}

$("demo-voice").addEventListener("change", () => {
  if (!$("demo-voice").checked) {
    $("say-status").textContent = "Demo voice off — new clips wait for you.";
    return;
  }
  const audio = $("say-audio");
  if (!audio.src) {
    $("say-status").textContent =
      "Demo voice armed. The next synthesized clip will play automatically.";
    return;
  }
  audio
    .play()
    .then(() => {
      $("say-status").textContent = "Demo voice on — playing latest clip.";
    })
    .catch((error) => {
      $("say-status").textContent =
        `Demo voice on, but the browser blocked playback (${error.name || "play failed"}).`;
    });
});

let voiceBusy = false;

$("say-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (voiceBusy) return;
  const value = $("say-text").value.trim();
  if (!value) return;
  voiceBusy = true;
  $("say-send").disabled = true;
  try {
    const result = await postJSON("/say", { text: value });
    if (result.command_id && typeof result.url === "string") {
      lastSeenClipId = result.command_id;
      wireSayClip(result.command_id, result.url, result.duration_s, value, result.playback);
      if (result.playback === "native") {
        activeNativeId = result.command_id;
        $("say-native").textContent = renderNativeStatus(result);
        $("say-status").textContent = "Native audio selected - clip plays on the computer default output, not in this tab.";
        return;
      }
      $("say-status").textContent =
        "Clip synthesized. Press play — browsers may block automatic audio.";
      if ($("demo-voice").checked) {
        $("say-audio")
          .play()
          .then(() => {
            $("say-status").textContent = "Demo voice on — playing your clip.";
          })
          .catch((error) => {
            $("say-status").textContent =
              `Demo voice on, but playback was blocked (${error.name || "play failed"}).`;
          });
      }
    } else {
      $("say-status").textContent = "Speech request accepted by the backend.";
    }
  } catch (error) {
    $("say-status").textContent = `Speech failed: ${error.message}`;
  } finally {
    voiceBusy = false;
    $("say-send").disabled = false;
  }
});

function base64FromBytes(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize)
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  return btoa(binary);
}

let transcribeBusy = false;

$("transcribe-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (transcribeBusy) return;
  const file = $("transcribe-file").files[0];
  const result = $("transcribe-result");
  if (!file) {
    result.textContent = "Choose a WAV file first.";
    return;
  }
  if (file.size > 4 * 1024 * 1024) {
    result.textContent = "File exceeds the 4 MB limit.";
    return;
  }
  transcribeBusy = true;
  $("transcribe-send").disabled = true;
  result.textContent = "Transcribing…";
  try {
    const data = await postJSON($("transcribe-provider").value === 'local' ? '/transcribe-local' : '/transcribe', {
      audio_b64: base64FromBytes(await file.arrayBuffer()),
      format: "wav",
      source: "simulation_audio",
      utterance_id: crypto.randomUUID(),
    });
    result.textContent = `Heard: ${String(data.text || "(no speech)")} · ${data.provider?.model || data.model} · ${Math.round(data.latency_ms)} ms · synthetic upload; not an incident reply`;
  } catch (error) {
    result.textContent = `Transcription failed: ${error.message}`;
  } finally {
    transcribeBusy = false;
    $("transcribe-send").disabled = false;
  }
});

let replyBusy = false;
let replyEventId = null;
async function pollCheckin() {
  try {
    const response = await fetch('/checkin-state', {cache: 'no-store'});
    if (!response.ok) throw new Error('Check-in API unavailable');
    const data = await response.json();
    const pending = data.pending_checkin;
    const open = pending?.phase === 'awaiting_reply' && Date.now() < pending.deadline_at;
    replyEventId = open ? pending.event_id : null;
    $('reply-window').textContent = open
      ? `Reply window: ${Math.max(0, (pending.deadline_at - Date.now()) / 1000).toFixed(1)} s remaining`
      : pending ? 'Waiting for question playback to finish.' : 'No active check-in.';
    document.querySelectorAll('[data-reply]').forEach(button => { button.disabled = !open || replyBusy; });
  } catch (error) {
    replyEventId = null;
    $('reply-window').textContent = error.message;
    document.querySelectorAll('[data-reply]').forEach(button => { button.disabled = true; });
  }
}
document.querySelectorAll('[data-reply]').forEach(button => {
  button.addEventListener('click', async () => {
    if (replyBusy || !replyEventId) return;
    replyBusy = true;
    document.querySelectorAll('[data-reply]').forEach(item => { item.disabled = true; });
    $('resident-reply-result').textContent = 'Replaying WAV and recognizing locally…';
    try {
      const data = await postJSON('/resident-reply', {fixture: button.dataset.reply, event_id: replyEventId});
      const decision = data.decision;
      $('resident-reply-result').textContent = `Whisper heard: ${data.recognition.text || '(no speech)'} · ${Math.round(data.recognition.latency_ms)} ms · ${decision.intent || decision.reason} · ${decision.applied ? 'applied to this check-in' : 'not applied'}`;
    } catch (error) {
      $('resident-reply-result').textContent = `Reply failed: ${error.message}`;
    } finally {
      replyBusy = false;
      pollCheckin();
    }
  });
});

async function voiceLoop() {
  await pollQueuedClips();
  pollBrain();
  pollCheckin();
  pollDemo();
  setTimeout(voiceLoop, 500);
}
voiceLoop();

function renderExploration(data) {
  const sameScene = !data.context_map_id || data.context_map_id === current?.map_id;
  const progress = sameScene ? data.progress : null;
  $('exploration-progress').hidden = !progress;
  if (progress) {
    const visits = progress.completed_visits || [];
    const names = [...new Set(visits.map(visit => visit.waypoint_id))];
    const label = value => String(value || '').replaceAll('-', ' ');
    $('exploration-summary').textContent = progress.active_waypoint && progress.navigation_state === 'moving'
      ? `${label(progress.navigation_state)} · target: ${label(progress.active_waypoint)}`
      : `${names.length} room${names.length === 1 ? '' : 's'} visited · ${label(progress.navigation_state || 'idle')}`;
    $('visited-rooms').replaceChildren(...names.map(name => {
      const chip = document.createElement('span');
      chip.textContent = `✓ ${label(name)}`;
      return chip;
    }));
    $('unvisited-rooms').textContent = progress.unvisited_waypoints?.length
      ? `Not yet visited: ${progress.unvisited_waypoints.map(label).join(', ')}` : '';
  }
  const age = Number.isFinite(data.updated_at) ? Math.max(0, Date.now() - data.updated_at) : 0;
  $('planner-freshness').hidden = sameScene && age < 15000;
  $('planner-freshness').textContent = !sameScene ? 'Planner history belongs to a previous scene. Waiting for this scene’s first decision.' : age < 60000
    ? `Last planner update ${Math.floor(age / 1000)} s ago. Shown actions are historical.`
    : `Last planner update ${Math.floor(age / 60000)} min ago. Shown actions are historical.`;
  const sighting = progress?.last_person_sighting;
  $('last-person-sighting').textContent = sighting
    ? `Last person sighting · ${new Date(sighting.ts).toLocaleTimeString()} · ${sighting.caption} · frame …${String(sighting.frame_id).slice(-8)}. Historical evidence; current presence is unconfirmed.` : '';
  const citations = data.memory?.citations || [];
  $('memory-citations').replaceChildren(...citations.slice(-4).map(citation => {
    const row = document.createElement('li');
    const time = citation.ts ? new Date(citation.ts).toLocaleTimeString() : 'Saved observation';
    row.textContent = `${time} · ${citation.caption || 'No caption'}${citation.frame_id ? ' · frame …' + String(citation.frame_id).slice(-8) : ''}`;
    return row;
  }));
}

function activateToolTab(tab, focus = false) {
  document.querySelectorAll('[role="tab"]').forEach(item => {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
    $(item.getAttribute('aria-controls')).hidden = !selected;
  });
  if (focus) tab.focus();
}
const toolTabs = [...document.querySelectorAll('[role="tab"]')];
toolTabs.forEach((tab, index) => {
  tab.addEventListener('click', () => activateToolTab(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % toolTabs.length;
    if (event.key === 'ArrowLeft') next = (index - 1 + toolTabs.length) % toolTabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = toolTabs.length - 1;
    if (next !== undefined) {
      event.preventDefault();
      activateToolTab(toolTabs[next], true);
    }
  });
});

async function pollDemo() {
  try {
    const response = await fetch('/demo-state', {cache:'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    if (demoStarting) return;
    const running = data.status === 'running';
    $('demo-evidence-panel').dataset.status = data.status;
    $('demo-run-status').textContent = {
      running:'Demonstrating live…', passed:'Last full-house run passed',
      failed:'Run incomplete', not_started:'Ready to demonstrate'
    }[data.status] || data.status;
    $('start-house-demo').disabled = running;
    $('start-house-demo').textContent = running ? 'Demo in progress…' : 'Run full-house demo →';
    const stages = data.stages || [];
    $('demo-stage-count').textContent = `${stages.length} steps`;
    const duration = data.started_at
      ? Math.max(0, ((data.finished_at || (running ? Date.now() : data.updated_at)) - data.started_at) / 1000) : null;
    $('demo-summary').textContent = stages.length
      ? `${stages.length} recorded steps${Number.isFinite(duration) ? ' · ' + duration.toFixed(1) + ' s' : ''}`
      : 'Camera → check-in → reply → family';
    // Update the live region only when evidence changes, keeping scrolling and speech stable.
    const signature = JSON.stringify([data.run_id, stages]);
    if (signature !== demoStagesSignature) {
      demoStagesSignature = signature;
      $('demo-stages').replaceChildren(...stages.map(stage => {
        const li = document.createElement('li');
        const time = document.createElement('time');
        time.textContent = `${((stage.ts - data.started_at) / 1000).toFixed(1)} s`;
        const detail = document.createElement('span');
        detail.textContent = `${stage.detail}${stage.text ? ' — ' + stage.text : ''}`;
        li.append(time, detail);
        return li;
      }));
    }
    if (running && previousDemoStatus !== 'running') $('demo-receipts').open = true;
    if (previousDemoStatus !== data.status && data.status !== 'failed') setPanelError('demo-error', null);
    previousDemoStatus = data.status;
    $('demo-run-detail').textContent = data.error || data.last_error || (data.model
      ? `${data.model} · ${data.inferences || 0} image inferences · ${data.last_plan?.execution || 'Waiting for first action'}`
      : 'Each step appears only when its result arrives.');
    if (data.status === 'failed') setPanelError('demo-error', data.error || data.last_error || 'The run stopped before every step completed. See run details.');
  } catch (_) { /* The renderer remains usable when the separate report is unavailable. */ }
}
$('start-house-demo').addEventListener('click', async () => {
  if (demoStarting) return;
  demoStarting = true;
  $('start-house-demo').disabled = true;
  $('start-house-demo').textContent = 'Starting demo…';
  setPanelError('demo-error', null);
  try {
    await postJSON('/demo/start', {});
    $('demo-run-status').textContent = 'Starting demonstration…';
    $('demo-receipts').open = true;
  } catch (error) {
    setPanelError('demo-error', error.message);
  } finally {
    demoStarting = false;
    $('start-house-demo').disabled = false;
    await pollDemo();
  }
});

function renderSpatial(state) {
  const sensor = state.spatial;
  for (const name of ['lidar', 'trajectory', 'route']) {
    const input = $('show-' + name);
    const pending = pendingOverlays.get(name);
    if (pending && (state.overlays?.[name] === pending.value || Date.now() - pending.started > 3000)) {
      if (state.overlays?.[name] !== pending.value) notice('Layer update was not acknowledged by the viewer.', true);
      pendingOverlays.delete(name);
    }
    input.disabled = !state.overlays || pendingOverlays.has(name);
    if (state.overlays && !pendingOverlays.has(name)) input.checked = !!state.overlays[name];
  }
  $('lidar-summary').textContent = sensor?.error || (sensor?.timestamp
    ? `${sensor.points_world?.length || 0} hits · simulated raycast`
    : 'Simulated LiDAR awaiting scan');
}
for (const name of ['lidar', 'trajectory', 'route']) {
  $('show-' + name).addEventListener('change', async event => {
    const input = event.target;
    const desired = input.checked;
    pendingOverlays.set(name, {value:desired, started:Date.now()});
    input.disabled = true;
    try { await postJSON('/control', {action:'overlays', [name]:desired}); }
    catch (error) {
      pendingOverlays.delete(name);
      input.checked = !desired;
      input.disabled = false;
      notice(error.message, true);
    }
  });
}
