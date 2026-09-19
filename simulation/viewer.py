"""Loopback-only live MuJoCo renderer; no hardware or network robot adapter."""

from __future__ import annotations

import argparse
import io
import json
import math
import mimetypes
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

WEB = Path(__file__).resolve().parent / "web"
PRESETS = {"front": (180, -20, 1.6), "side": (90, -20, 1.6), "top": (90, -89, 1.8)}


def validate_control(value):
    if not isinstance(value, dict) or value.get("action") not in {
        "play",
        "pause",
        "reset",
        "step",
        "camera",
        "hold",
    }:
        raise ValueError("action must be play, pause, reset, step, camera, or hold")
    if value["action"] == "hold":
        if set(value) != {"action", "enabled"} or not isinstance(
            value["enabled"], bool
        ):
            raise ValueError("hold requires boolean enabled")
        return value
    if value["action"] != "camera":
        if set(value) != {"action"}:
            raise ValueError("unexpected control fields")
        return value
    if "preset" in value:
        if set(value) != {"action", "preset"} or value["preset"] not in PRESETS:
            raise ValueError("preset must be front, side, or top")
    else:
        if (
            not set(value) <= {"action", "azimuth", "elevation", "distance"}
            or len(value) < 2
        ):
            raise ValueError("camera requires preset or bounded camera coordinates")
        for name, bounds in {
            "azimuth": (-360, 360),
            "elevation": (-90, 90),
            "distance": (0.4, 10),
        }.items():
            if name in value:
                number = value[name]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(number)
                    or not bounds[0] <= number <= bounds[1]
                ):
                    raise ValueError(
                        f"{name} must be a finite number between {bounds[0]} and {bounds[1]}"
                    )
    return value


def finite_number(value):
    value = float(value)
    return value if math.isfinite(value) else None


def physics_fault(data, mujoco):
    """Inspect immediately after stepping: MuJoCo may auto-reset bad states."""
    for name in ("BADQPOS", "BADQVEL", "BADQACC", "BADCTRL"):
        warning = getattr(mujoco.mjtWarning, "mjWARN_" + name)
        if data.warning[warning].number:
            return f"MuJoCo {name} warning; reset required"
    if not math.isfinite(float(data.time)) or any(
        not math.isfinite(float(value))
        for values in (data.qpos, data.qvel)
        for value in values
    ):
        return "Non-finite simulation state; reset required"
    return None


class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.commands = queue.Queue(maxsize=100)
        self.state = {"ready": False, "render_error": None, "physics_error": None}
        self.frame = None


def handler_for(shared, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body, content_type="application/json"):
            if content_type == "application/json":
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self, write=False):
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.reply(403, {"error": "loopback Host required"})
                return False
            origin = self.headers.get("Origin")
            if write and origin is not None and origin != f"http://{host}":
                self.reply(403, {"error": "same-origin control required"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            if path == "/state":
                with shared.lock:
                    state = dict(shared.state)
                self.reply(200, state)
            elif path == "/favicon.ico":
                self.reply(204, b"", "image/x-icon")
            elif path == "/frame.jpg":
                with shared.lock:
                    frame = shared.frame
                if frame is None:
                    self.reply(503, {"error": "frame not ready"})
                else:
                    self.reply(200, frame, "image/jpeg")
            else:
                try:
                    relative = unquote(path).lstrip("/") or "index.html"
                    target = (WEB / relative).resolve()
                    if (
                        not target.is_relative_to(WEB.resolve())
                        or target.suffix not in {".html", ".css", ".js"}
                        or not target.is_file()
                    ):
                        raise ValueError()
                    content = target.read_bytes()
                except (ValueError, OSError):
                    self.reply(404, {"error": "not found"})
                    return
                self.reply(
                    200,
                    content,
                    mimetypes.guess_type(target)[0] or "application/octet-stream",
                )

        def do_POST(self):
            if not self.allowed(write=True):
                return
            if urlsplit(self.path).path != "/control":
                self.reply(404, {"error": "not found"})
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("transfer encoding is unsupported")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("body must contain 1–2048 bytes")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("application/json required")
                self.connection.settimeout(2)
                command = validate_control(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError, OSError):
                self.close_connection = True
                self.reply(422, {"error": "invalid control JSON or fields"})
                return
            try:
                shared.commands.put_nowait(command)
            except queue.Full:
                self.reply(429, {"error": "control queue full"})
                return
            with shared.lock:
                state = dict(shared.state)
            self.reply(202, {"queued": True, "state": state})

    return Handler


def run(args):
    import mujoco
    from PIL import Image

    model = mujoco.MjModel.from_xml_path(str(args.model.resolve()))
    if not math.isfinite(float(model.opt.timestep)) or model.opt.timestep <= 0:
        raise ValueError("model timestep must be finite and positive")
    data = mujoco.MjData(model)
    camera = mujoco.MjvCamera()
    mujoco.mjv_defaultCamera(camera)
    camera.azimuth, camera.elevation, camera.distance = PRESETS["side"]
    renderer = mujoco.Renderer(model, height=480, width=640)
    shared = Shared()
    running, steps = False, 0
    # Position servos use affine joint-position feedback; torque motors do not.
    position_servos = bool(model.nu) and all(model.actuator_biasprm[:, 1] < 0)
    hold = False
    joints = model.actuator_trnid[:, 0]
    hold_supported = (
        bool(model.nu)
        and not position_servos
        and all(
            model.actuator_trntype[i] == mujoco.mjtTrn.mjTRN_JOINT
            and model.jnt_type[joints[i]] == mujoco.mjtJoint.mjJNT_HINGE
            and model.actuator_gear[i, 0] == 1
            for i in range(model.nu)
        )
    )

    def reset():
        if model.nkey:
            mujoco.mj_resetDataKeyframe(model, data, 0)
        else:
            mujoco.mj_resetData(model, data)
        mujoco.mj_forward(model, data)

    reset()
    nominal_ctrl = data.ctrl.copy()
    targets = data.qpos[model.jnt_qposadr[joints]].copy() if hold_supported else None

    def controls():
        if position_servos:
            data.ctrl[:] = nominal_ctrl
        elif hold and hold_supported:
            torque = (
                40 * (targets - data.qpos[model.jnt_qposadr[joints]])
                - 2 * data.qvel[model.jnt_dofadr[joints]]
            )
            for i in range(model.nu):
                lo, hi = model.actuator_ctrlrange[i]
                data.ctrl[i] = (
                    max(lo, min(hi, torque[i]))
                    if model.actuator_ctrllimited[i]
                    else torque[i]
                )
        else:
            data.ctrl[:] = 0

    controls()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(shared, args.port))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(
        f"MuJoCo {mujoco.__version__}: http://{args.host}:{args.port} (paused)",
        flush=True,
    )
    previous, next_frame = time.monotonic(), 0.0
    accumulator = 0.0
    render_error = None
    physics_error = physics_fault(data, mujoco)

    def step():
        nonlocal steps, physics_error, running, accumulator
        if physics_error:
            return
        try:
            controls()
            mujoco.mj_step(model, data)
            steps += 1
            physics_error = physics_fault(data, mujoco)
        except Exception as exc:  # noqa: BLE001 - halt physics and report native runtime failures
            physics_error = f"{type(exc).__name__}: physics step failed; reset required"
        if physics_error:
            running = False
            accumulator = 0.0

    try:
        while True:
            now = time.monotonic()
            elapsed = min(now - previous, 0.05)
            previous = now
            for _ in range(100):
                try:
                    command = shared.commands.get_nowait()
                except queue.Empty:
                    break
                action = command["action"]
                if action in {"play", "pause"}:
                    running = action == "play" and physics_error is None
                    accumulator = 0.0
                elif action == "hold":
                    hold = command["enabled"] and hold_supported
                elif action == "reset":
                    reset()
                    physics_error = physics_fault(data, mujoco)
                    running, steps, accumulator = False, 0, 0.0
                elif action == "step":
                    running = False
                    step()
                elif "preset" in command:
                    camera.azimuth, camera.elevation, camera.distance = PRESETS[
                        command["preset"]
                    ]
                else:
                    for name in ("azimuth", "elevation", "distance"):
                        if name in command:
                            setattr(camera, name, command[name])
            if physics_error is None:
                controls()
            if running:
                accumulator += elapsed
                for _ in range(min(int(accumulator / model.opt.timestep), 100)):
                    step()
                    if physics_error:
                        break
                    accumulator = max(0.0, accumulator - model.opt.timestep)
            if now >= next_frame and physics_error is None:
                next_frame = now + 0.1
                try:
                    camera.lookat[:] = (
                        data.qpos[:3] if model.nq >= 7 else model.stat.center
                    )
                    renderer.update_scene(data, camera=camera)
                    output = io.BytesIO()
                    Image.fromarray(renderer.render()).save(
                        output, format="JPEG", quality=85
                    )
                    with shared.lock:
                        shared.frame = output.getvalue()
                    render_error = None
                except Exception as exc:  # noqa: BLE001 - surface renderer failures without losing controls
                    render_error = f"{type(exc).__name__}: rendering failed"
                    running = False
            with shared.lock:
                shared.state = {
                    "ready": shared.frame is not None,
                    "simulation_time": finite_number(data.time),
                    "running": running,
                    "steps": steps,
                    "mujoco_version": mujoco.__version__,
                    "modelname": model.names.split(b"\x00", 1)[0].decode(
                        "utf-8", errors="replace"
                    )
                    or args.model.stem,
                    "physics_dt": float(model.opt.timestep),
                    "qpos_base": [finite_number(v) for v in data.qpos[:7]],
                    "nu": model.nu,
                    "source": "direct_mujoco",
                    "control_mode": (
                        "keyframe joint position targets; no gait controller"
                        if position_servos
                        else "PD joint hold (kp=40, kd=2); no gait controller"
                        if hold
                        else "passive physics (zero motor torque); no gait controller"
                    ),
                    "hold_enabled": hold,
                    "hold_supported": hold_supported,
                    "physics_error": physics_error,
                    "render_error": render_error,
                    "frame_width": 640,
                    "frame_height": 480,
                }
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        renderer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1"
    )
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    run(args)


if __name__ == "__main__":
    main()
