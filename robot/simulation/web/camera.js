/* Interactive operator camera. Camera-only controls never issue robot missions. */
"use strict";
const cameraController = (() => {
  const viewport = document.getElementById('scene-viewport');
  const hint = document.getElementById('camera-help');
  const pointers = new Map();
  let camera = null;
  let pending = null;
  let inFlight = false;
  let timer = null;
  let lastInput = 0;
  let gesture = null;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const copy = value => ({...value, lookat: [...value.lookat]});
  const wrap = value => ((value + 180) % 360 + 360) % 360 - 180;

  function showHelp(message) {
    hint.textContent = message || 'Drag to orbit · Shift / right-drag to pan · Scroll to zoom';
  }
  function sync(state) {
    if (state.camera && !pointers.size && !pending && !inFlight && Date.now() - lastInput > 500) {
      camera = copy(state.camera);
    }
    viewport.setAttribute('aria-disabled', String(!camera));
    for (const id of ['camera-reset', 'camera-zoom-in', 'camera-zoom-out']) {
      document.getElementById(id).disabled = !camera;
    }
    if (!camera) showHelp('Interactive camera is waiting for the viewer.');
  }
  async function flush() {
    timer = null;
    if (inFlight || !pending) return;
    const command = pending;
    pending = null;
    inFlight = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2500);
    try {
      const response = await fetch('/control', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(command), signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Camera update rejected (${response.status})`);
      showHelp();
      document.querySelectorAll('[data-camera]').forEach(button => button.setAttribute('aria-pressed', 'false'));
    } catch (error) {
      pending = null;
      showHelp(error.name === 'AbortError' ? 'Camera update timed out. Try dragging again.' : error.message);
    } finally {
      clearTimeout(timeout);
      inFlight = false;
      if (pending) timer = setTimeout(flush, 50);
    }
  }
  function queue() {
    if (!camera) return;
    lastInput = Date.now();
    pending = {action:'camera', ...copy(camera)};
    if (!timer && !inFlight) timer = setTimeout(flush, 50);
  }
  function orbit(dx, dy) {
    if (!camera) return;
    camera.azimuth = wrap(camera.azimuth - dx * .35);
    camera.elevation = clamp(camera.elevation - dy * .3, -89, 15);
    queue();
  }
  function pan(dx, dy) {
    if (!camera) return;
    const azimuth = camera.azimuth * Math.PI / 180;
    const elevation = camera.elevation * Math.PI / 180;
    const scale = camera.distance * 2 * Math.tan(22.5 * Math.PI / 180) / viewport.clientHeight;
    // Camera-right and camera-up basis for MuJoCo's z-up orbit camera.
    const right = [-Math.sin(azimuth), Math.cos(azimuth), 0];
    const up = [-Math.sin(elevation) * Math.cos(azimuth), -Math.sin(elevation) * Math.sin(azimuth), Math.cos(elevation)];
    camera.lookat = camera.lookat.map((value, index) => clamp(value - dx * scale * right[index] + dy * scale * up[index], -100, 100));
    queue();
  }
  function zoom(factor) {
    if (!camera) return;
    camera.distance = clamp(camera.distance * factor, .4, 40);
    queue();
  }
  function rebaseGesture() {
    const points = [...pointers.values()];
    gesture = points.length >= 2 ? {
      x:(points[0].x + points[1].x) / 2,
      y:(points[0].y + points[1].y) / 2,
      distance:Math.hypot(points[0].x-points[1].x, points[0].y-points[1].y),
    } : null;
  }
  viewport.addEventListener('pointerdown', event => {
    if (!camera || event.target.closest('button')) return;
    event.preventDefault();
    viewport.focus({preventScroll:true});
    viewport.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, {x:event.clientX, y:event.clientY, pan:event.button === 2 || event.button === 1 || event.shiftKey});
    viewport.classList.add('dragging');
    rebaseGesture();
  });
  viewport.addEventListener('pointermove', event => {
    const previous = pointers.get(event.pointerId);
    if (!previous) return;
    pointers.set(event.pointerId, {...previous, x:event.clientX, y:event.clientY});
    if (pointers.size >= 2) {
      const old = gesture;
      rebaseGesture();
      if (old && old.distance > 0 && gesture.distance > 0) {
        pan(gesture.x-old.x, gesture.y-old.y);
        zoom(old.distance/gesture.distance);
      }
    } else if (previous.pan || event.shiftKey) {
      pan(event.clientX-previous.x, event.clientY-previous.y);
    } else {
      orbit(event.clientX-previous.x, event.clientY-previous.y);
    }
  });
  function release(event) {
    pointers.delete(event.pointerId);
    if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
    if (!pointers.size) viewport.classList.remove('dragging');
    rebaseGesture();
  }
  viewport.addEventListener('pointerup', release);
  viewport.addEventListener('pointercancel', release);
  viewport.addEventListener('lostpointercapture', event => {
    pointers.delete(event.pointerId);
    if (!pointers.size) viewport.classList.remove('dragging');
    rebaseGesture();
  });
  viewport.addEventListener('contextmenu', event => event.preventDefault());
  viewport.addEventListener('dragstart', event => event.preventDefault());
  viewport.addEventListener('wheel', event => {
    if (!camera) return;
    event.preventDefault();
    const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1);
    zoom(Math.exp(clamp(pixels, -200, 200) * .002));
  }, {passive:false});
  async function resetView() {
    if (!camera) return;
    pending = null;
    if (timer) clearTimeout(timer);
    timer = null;
    // The reset supersedes unsent movement and follows any request already in flight.
    pending = {action:'camera', preset:document.querySelector('[data-camera="room"]').disabled ? 'side' : 'room'};
    lastInput = 0;
    if (!inFlight) await flush();
  }
  viewport.addEventListener('dblclick', event => { if (!event.target.closest('button')) resetView(); });
  viewport.addEventListener('keydown', event => {
    if (event.target !== viewport || !camera) return;
    const arrows = {ArrowLeft:[-18,0], ArrowRight:[18,0], ArrowUp:[0,-18], ArrowDown:[0,18]};
    if (arrows[event.key]) {
      event.preventDefault();
      (event.shiftKey ? pan : orbit)(...arrows[event.key]);
    } else if (['+', '=', '-', 'r', 'R'].includes(event.key)) {
      event.preventDefault();
      if (event.key.toLowerCase() === 'r') resetView();
      else zoom(event.key === '-' ? 1.15 : 1 / 1.15);
    }
  });
  document.getElementById('camera-reset').addEventListener('click', resetView);
  document.getElementById('camera-zoom-in').addEventListener('click', () => zoom(1 / 1.2));
  document.getElementById('camera-zoom-out').addEventListener('click', () => zoom(1.2));
  return {sync};
})();
