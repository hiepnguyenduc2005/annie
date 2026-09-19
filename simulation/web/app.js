"use strict";
const $ = (id) => document.getElementById(id);
let current = null;
let busy = false;
let rendered = 0;
let rateStart = performance.now();
let wasConnected = false;

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
    notice(
      `${payload.action === "camera" ? "Camera change" : "Simulation control"} accepted.`,
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
    $("version").textContent =
      `MuJoCo ${current.mujoco_version ?? ""} · Unitree Go2 · local rendering`;
    if (current.control_mode)
      $("control-mode").textContent = current.control_mode;
    if (typeof current.hold_enabled === "boolean")
      $("hold").checked = current.hold_enabled;
    if (current.render_error) notice(current.render_error, true);
    if (current.physics_error) notice(current.physics_error, true);
    $("hold").disabled = !current.hold_supported;
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
$("hold").addEventListener("change", () =>
  control({ action: "hold", enabled: $("hold").checked }),
);
for (const button of document.querySelectorAll("[data-camera]")) {
  button.addEventListener("click", () =>
    control({ action: "camera", preset: button.dataset.camera }),
  );
}
document.addEventListener("keydown", (event) => {
  if (
    event.code === "Space" &&
    !["INPUT", "BUTTON", "A"].includes(document.activeElement.tagName)
  ) {
    event.preventDefault();
    if (current) control({ action: current.running ? "pause" : "play" });
  }
});
async function stateLoop() {
  await pollState();
  setTimeout(stateLoop, 500);
}
stateLoop();
nextFrame();
