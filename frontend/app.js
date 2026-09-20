"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  map: null,
  events: [],
  token: "",
  socket: null,
  retry: null,
  stopped: false,
  thread: [],
  runs: {},
  familySocket: null,
  familyRetry: null,
};
const names = {
  fall_suspected: "Possible floor incident — check-in needed",
  fall_confirmed: "Family attention needed",
  checkin_ok: "Resident reassurance received",
  checkin_no_reply: "No reassurance received",
  reminder_due: "Reminder due",
};
function notice(text, error = false) {
  $("notice").hidden = false;
  $("notice").textContent = text;
  $("notice").className = error ? "error" : "";
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {}
    throw new Error(
      typeof detail === "string"
        ? detail
        : `Request failed (${response.status})`,
    );
  }
  return response.json();
}
function text(id, value) {
  $(id).textContent = value;
}
function when(ts) {
  return new Date(ts).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
function renderStatus() {
  const status = state.status;
  if (!status) return;
  const dog = status.dog,
    obs = status.perception;
  const simulated = !!dog?.pose?.map_id?.startsWith("sim-");
  text(
    "mode",
    simulated
      ? "SIMULATION"
      : status.mode === "demo"
        ? "SYNTHETIC DEMO"
        : "LIVE INPUT MODE",
  );
  text("robot-state", dog ? `Annie is ${dog.state}` : "No robot observations");
  text(
    "robot-detail",
    dog
      ? `${dog.battery_pct}% battery${simulated ? " (synthetic, not measured)" : ""} · ${dog.waypoint || "No waypoint set"} · reported ${when(dog.ts)}${simulated ? " · simulated telemetry" : ""}`
      : "Robot adapter not connected",
  );
  text(
    "observation-title",
    obs ? `${obs.posture} · ${obs.location}` : "Nothing observed yet",
  );
  const sources = {
    mock: "synthetic mock",
    simulation_ground_truth: "simulation ground truth",
    simulation_vlm: "simulated camera + VLM",
    hardware_vlm: "hardware camera + VLM",
  };
  text(
    "observation-detail",
    obs
      ? [
          obs.caption,
          `${Math.round(obs.confidence * 100)}% reported confidence`,
          obs.source ? sources[obs.source] ?? obs.source : null,
          obs.model ? `model ${obs.model}` : null,
          when(obs.ts),
        ]
          .filter(Boolean)
          .join(" · ")
      : "Evidence will appear here",
  );
  document.querySelectorAll("[data-scenario],#seed").forEach((b) => {
    b.disabled = status.mode !== "demo";
  });
  renderCheckin();
  renderMap();
}
function renderCheckin() {
  const pending = state.status?.pending_checkin;
  text(
    "checkin-title",
    pending ? "Waiting for reassurance" : "No active check-in",
  );
  text(
    "checkin-detail",
    pending
      ? `${Math.max(0, Math.ceil((pending.deadline_at - Date.now()) / 1000))}s remaining · response must match this event`
      : "A family acknowledgement is not a safety confirmation",
  );
}
function svgEl(tag, attrs = {}, content) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  if (content !== undefined) e.textContent = content;
  return e;
}
function renderMap() {
  const map = state.map;
  if (!map) return;
  const box = $("map");
  box.replaceChildren();
  const rooms = map.rooms || [];
  const pose = state.status?.dog?.pose;
  const xs = [];
  const ys = [];
  for (const room of rooms) {
    xs.push(room.x, room.x + room.width);
    ys.push(room.y, room.y + room.height);
  }
  for (const point of map.waypoints || []) {
    xs.push(point.x);
    ys.push(point.y);
  }
  if (pose && pose.map_id === map.map_id) {
    xs.push(pose.x);
    ys.push(pose.y);
  }
  if (!xs.length) {
    xs.push(0, 8);
    ys.push(0, 4);
  }
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const maxX = Math.max(...xs);
  const maxY = Math.max(...ys);
  const svg = svgEl("svg", {
    viewBox: `${minX - 0.3} ${minY - 0.3} ${maxX - minX + 0.6} ${maxY - minY + 0.6}`,
    role: "img",
    "aria-label": "Home layout and robot position",
  });
  for (const room of rooms) {
    svg.append(
      svgEl("rect", {
        x: room.x,
        y: room.y,
        width: room.width,
        height: room.height,
        rx: 0.12,
        fill: "#e4e9db",
        stroke: "#bdcbb6",
        "stroke-width": 0.045,
      }),
    );
    svg.append(
      svgEl(
        "text",
        {
          x: room.x + 0.28,
          y: room.y + 0.47,
          "font-size": 0.22,
          fill: "#64775d",
        },
        room.label,
      ),
    );
  }
  for (const point of map.waypoints || []) {
    svg.append(
      svgEl("circle", { cx: point.x, cy: point.y, r: 0.075, fill: "#a1b294" }),
    );
  }
  if (pose && pose.map_id === map.map_id) {
    svg.append(
      svgEl("circle", {
        cx: pose.x,
        cy: pose.y,
        r: 0.3,
        fill: "#2e5b43",
        opacity: 0.13,
      }),
    );
    svg.append(
      svgEl("circle", {
        cx: pose.x,
        cy: pose.y,
        r: 0.14,
        fill: "#2e5b43",
        stroke: "white",
        "stroke-width": 0.06,
      }),
    );
  }
  box.append(svg);
  text("map-id", map.map_id);
}
function renderEvents() {
  text("event-count", `${state.events.length} events`);
  const el = $("events");
  el.replaceChildren();
  if (!state.events.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "No events yet. Safe bed-rest should stay quiet.";
    el.append(p);
    return;
  }
  for (const event of [...state.events].sort((a, b) => b.ts - a.ts)) {
    const item = document.createElement("article");
    item.className = `event ${event.severity}`;
    const heading = document.createElement("strong");
    heading.textContent = names[event.kind] || event.kind;
    const detail = document.createElement("p");
    detail.textContent = `${when(event.ts)} · ${event.acknowledged ? "Acknowledged by family" : event.severity} · evidence ${event.evidence.frame_id.slice(0, 8)}`;
    item.append(heading, detail);
    if (!event.acknowledged) {
      const button = document.createElement("button");
      button.textContent = "Acknowledge receipt";
      button.addEventListener("click", () =>
        action(button, async () => {
          await api(`/events/${event.event_id}/ack`, { by: "family" });
          await refresh();
        }),
      );
      item.append(button);
    }
    el.append(item);
  }
}
async function refresh() {
  const [status, events, map, commands] = await Promise.all([
    api("/status"),
    api("/events?since=0"),
    api("/map").catch((e) => {
      if (e.message === "No map received") return null;
      throw e;
    }),
    api("/commands"),
  ]);
  state.status = status;
  state.events = events;
  state.map = map;
  renderStatus();
  renderEvents();
  const last = commands.at(-1);
  if (last) {
    const ran = last.status && last.status !== "queued";
    text(
      "command-result",
      ran
        ? `${last.cmd} ${last.status} via ${last.source ?? "bridge"}${last.detail ? `: ${last.detail}` : ""}. ID ${last.command_id.slice(0, 8)}.`
        : `Queued: ${last.text || last.cmd}. ID ${last.command_id.slice(0, 8)}.`,
    );
  }
}
async function action(button, fn) {
  button.disabled = true;
  try {
    await fn();
  } catch (e) {
    notice(e.message, true);
  } finally {
    button.disabled = false;
  }
}
const runLabels = {
  dispatched: "Sending to Annie…",
  running: "Annie is on it",
  completed: "Delivered",
  failed: "Annie could not finish",
  unreachable: "Could not reach Annie",
};
const speakerNames = { annie: "Annie", resident: "Grandma" };
function renderFamily() {
  const target = $("family-thread");
  target.replaceChildren();
  if (!state.thread.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No messages yet.";
    target.append(empty);
    return;
  }
  for (const message of state.thread) {
    const block = document.createElement("div");
    block.className = "family-msg";
    const sent = document.createElement("p");
    sent.className = "family-sent";
    sent.textContent = message.text;
    block.append(sent);

    const run = state.runs[message.run_id];
    if (run) {
      const status = document.createElement("span");
      status.className = `run-status run-${run.status}`;
      status.textContent = runLabels[run.status] || run.status;
      block.append(status);
      for (const event of run.events) {
        const beat = document.createElement("div");
        beat.className = `beat beat-${event.speaker}`;
        if (speakerNames[event.speaker]) {
          const who = document.createElement("span");
          who.className = "beat-who";
          who.textContent = speakerNames[event.speaker];
          beat.append(who);
        }
        // Rendered from the backend's summary, never by reaching into the
        // robot-supplied payload, so an unfamiliar payload still reads.
        const line = document.createElement("span");
        line.textContent = event.summary;
        beat.append(line);
        block.append(beat);
      }
    }
    target.append(block);
  }
  target.scrollTop = target.scrollHeight;
}
function applyFamilySnapshot(data) {
  state.thread = data.thread || [];
  state.runs = {};
  for (const run of data.runs || []) state.runs[run.run_id] = run;
  renderFamily();
}
/// A second socket, separate from /live: family runs are their own stream.
function connectFamily() {
  clearTimeout(state.familyRetry);
  if (state.familySocket) {
    state.familySocket.onclose = null;
    state.familySocket.close();
  }
  const ws = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/family`,
  );
  state.familySocket = ws;
  ws.onopen = () => {
    if (state.token) ws.send(JSON.stringify({ token: state.token }));
  };
  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === "snapshot") {
      applyFamilySnapshot(message.data);
    } else if (message.type === "message") {
      state.thread.push(message.data);
      renderFamily();
    } else if (message.type === "run_status") {
      const run = state.runs[message.data.run_id];
      if (run) run.status = message.data.status;
      else refreshFamily().catch(() => {});
      renderFamily();
    } else if (message.type === "run_event") {
      const run = state.runs[message.data.run_id];
      if (run) {
        run.events.push(message.data);
        renderFamily();
      } else {
        refreshFamily().catch(() => {});
      }
    } else if (message.type === "resync") {
      refreshFamily().catch(() => {});
    }
  };
  ws.onclose = () => {
    state.familyRetry = setTimeout(connectFamily, 3000);
  };
}
async function refreshFamily() {
  state.thread = await api("/api/thread");
  for (const message of state.thread.slice(-5)) {
    state.runs[message.run_id] = await api(`/api/runs/${message.run_id}`);
  }
  renderFamily();
}
function connect() {
  clearTimeout(state.retry);
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
  }
  const ws = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/live`,
  );
  state.socket = ws;
  ws.onopen = () => {
    if (state.token) ws.send(JSON.stringify({ token: state.token }));
  };
  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    text("connection-text", "Connected");
    $("connection-dot").className = "online";
    if (message.type === "snapshot") {
      state.status = message.data;
      renderStatus();
    } else if (message.type === "checkin") {
      if (state.status) state.status.pending_checkin = message.data;
      renderCheckin();
    } else if (message.type === "dog.status") {
      if (state.status) state.status.dog = message.data;
      renderStatus();
    } else if (message.type === "brain.perception") {
      if (state.status) state.status.perception = message.data;
      renderStatus();
    } else if (message.type === "dog.map") {
      state.map = message.data;
      renderMap();
    } else if (["event", "command", "resync"].includes(message.type)) {
      refresh().catch((e) => notice(e.message, true));
    }
  };
  ws.onclose = () => {
    text("connection-text", "Disconnected");
    $("connection-dot").className = "";
    state.retry = setTimeout(() => {
      refresh()
        .then(connect)
        .catch((e) => {
          notice(e.message, true);
          state.retry = setTimeout(connect, 5000);
        });
    }, 3000);
  };
  ws.onerror = () => {
    text("connection-text", "Connection unavailable");
  };
}
$("seed").addEventListener("click", (e) =>
  action(e.currentTarget, async () => {
    await api("/demo/seed", {});
    await refresh();
    notice(
      "Synthetic home ready. Try bed-rest, then a possible floor incident.",
    );
  }),
);
for (const b of document.querySelectorAll("[data-scenario]"))
  b.addEventListener("click", () =>
    action(b, async () => {
      await api("/demo/scenario", { scenario: b.dataset.scenario });
      await refresh();
      notice(
        "Synthetic scenario applied. No real notification or voice playback.",
      );
    }),
  );
for (const b of document.querySelectorAll("[data-command]"))
  b.addEventListener("click", () =>
    action(b, async () => {
      await api("/commands", { cmd: b.dataset.command });
      await refresh();
      notice("Command submitted to the robot bridge.");
    }),
  );
$("say-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = $("message").value.trim();
  if (!value) return;
  action(e.submitter, async () => {
    // Returns as soon as the backend accepts it; the robot's errand is
    // reported afterwards through the run, never awaited here.
    const ack = await api("/api/messages", {
      author_id: $("author").value,
      text: value,
    });
    $("message").value = "";
    if (!state.runs[ack.run_id]) await refreshFamily();
    notice("Sent to Annie. Watch the errand below.");
  });
});
$("query-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = $("question").value.trim();
  if (!value) return;
  action(e.submitter, async () => {
    const result = await api("/query", { text: value });
    const target = $("answer");
    target.replaceChildren();
    const p = document.createElement("p");
    p.textContent = result.answer;
    target.append(p);
    for (const cite of result.citations) {
      const row = document.createElement("span");
      row.className = "citation";
      row.textContent = `${when(cite.ts)} · Frame ${cite.frame_id.slice(0, 8)} · Observer (${cite.pose.x}, ${cite.pose.y}) m in ${cite.pose.map_id} · ${cite.crop_url ? "Crop available" : "No image released"}`;
      target.append(row);
    }
  });
});
$("connect").addEventListener("click", (e) =>
  action(e.currentTarget, async () => {
    state.token = $("api-token").value.trim();
    await refresh();
    connect();
    connectFamily();
    notice("Connection settings applied for this page session.");
  }),
);
setInterval(renderCheckin, 250);
refresh()
  .then(connect)
  .catch((e) => notice(e.message, true));
refreshFamily()
  .then(connectFamily)
  .catch(() => connectFamily());
