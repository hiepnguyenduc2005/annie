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

function renderScene() {
  const scene = catalog.scenes.find((item) => item.id === current?.scene_id);
  if (scene) {
    $("scene-select").value = scene.id;
    $("scene-description").textContent = scene.description;
    $("ground-truth").textContent = JSON.stringify(scene.ground_truth, null, 2);
    if (activeSceneId !== scene.id) {
      activeSceneId = scene.id;
      notice(`Loaded: ${scene.title}. Simulation paused at its starting pose.`);
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
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
  }
}

async function pollState() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw new Error("Simulator unavailable");
    current = await response.json();
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
    $("connection").textContent = "Local simulator connected";
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
      : "No routine schedule loaded.";
    if (current.dropped_wall_seconds > 0.1)
      notice(
        `Rendering cannot keep up: ${current.dropped_wall_seconds.toFixed(2)} s of wall time skipped. Measured speed shows actual progress.`,
        true,
      );
    $("version").textContent =
      `MuJoCo ${current.mujoco_version ?? ""} · Unitree Go2 · local rendering`;
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
  button.addEventListener("click", () =>
    control({ action: "camera", preset: button.dataset.camera }),
  );
}
document.addEventListener("keydown", (event) => {
  if (
    event.code === "Space" &&
    !["INPUT", "SELECT", "BUTTON", "A"].includes(document.activeElement.tagName)
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
stateLoop();
loadCatalog();
nextFrame();
