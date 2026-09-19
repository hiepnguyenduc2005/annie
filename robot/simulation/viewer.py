"""Loopback-only live MuJoCo renderer; no hardware or network robot adapter."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import mimetypes
import queue
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

WEB = Path(__file__).resolve().parent / "web"
if __package__ in (None, ''):
    sys.path.insert(0, str(WEB.parent.parent.parent))
PRESETS = {"front": (180, -20, 1.6), "side": (90, -20, 1.6), "top": (90, -89, 1.8)}


def validate_control(value, scene_ids=()):
    if not isinstance(value, dict) or value.get("action") not in {
        "play",
        "pause",
        "reset",
        "step",
        "camera",
        "hold",
        "scene",
        "generate",
        "speed",
        "mission",
    }:
        raise ValueError("action must be play, pause, reset, step, camera, or hold")
    if value["action"] == "mission":
        if not set(value) <= {"action", "cmd", "waypoint", "command_id", "heading"} or value.get(
            "cmd"
        ) not in {"goto", "patrol", "stop", "resume", "look", "turn"}:
            raise ValueError("invalid mission")
        if value["cmd"] == "goto" and value.get("waypoint") not in {
            "home",
            "living-room",
            "bedroom",
            "hallway",
        }:
            raise ValueError("unknown waypoint")
        if value['cmd'] != 'goto' and 'waypoint' in value:
            raise ValueError('waypoint is only valid for goto')
        if value['cmd'] == 'turn':
            heading = value.get('heading')
            if type(heading) not in (int, float) or not math.isfinite(heading) or not -math.pi <= heading <= math.pi:
                raise ValueError('turn requires absolute heading in [-pi, pi]')
        elif 'heading' in value:
            raise ValueError('heading is only valid for turn')
        if "command_id" in value:
            UUID(value["command_id"])
        return value
    if value["action"] == "speed":
        if (
            set(value) != {"action", "value"}
            or type(value["value"]) not in (int, float)
            or value["value"] not in (0.25, 0.5, 1, 2)
        ):
            raise ValueError("speed must be 0.25, 0.5, 1, or 2")
        return value
    if value["action"] == "generate":
        if set(value) != {"action", "count", "seed"}:
            raise ValueError("generate requires count and seed")
        for key, lo, hi in (("count", 1, 96), ("seed", 0, 2147483647)):
            if type(value[key]) is not int or not lo <= value[key] <= hi:
                raise ValueError("invalid generation count or seed")
        return value
    if value["action"] == "scene":
        if (
            set(value) != {"action", "id"}
            or not isinstance(value["id"], str)
            or value["id"] not in scene_ids
        ):
            raise ValueError("scene id must exist in catalog")
        return value
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
        if set(value) != {"action", "preset"} or value["preset"] not in {
            *PRESETS,
            "room",
        }:
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
        self.observation = None
        self.robot_frame = None
        self.speech = {}
        self.speech_lock = threading.Lock()
        self.stt_lock = threading.Lock()
        self.stt = None


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
            elif path == "/brain-state":
                try:
                    status_file = Path(".data/simulation/bridge-status.json")
                    if status_file.stat().st_size > 32000:
                        raise ValueError()
                    self.reply(200, json.loads(status_file.read_text()))
                except (OSError, ValueError):
                    self.reply(503, {"last_error": "Brain bridge not connected"})
            elif path.startswith("/speech/") and path.endswith(".wav"):
                key = path.rsplit("/", 1)[-1][:-4]
                with shared.lock:
                    clip = shared.speech.get(key)
                if clip:
                    try:
                        self.reply(200, Path(clip["file_path"]).read_bytes(), "audio/wav")
                    except OSError:
                        self.reply(404, {"error": "clip unavailable"})
                else:
                    self.reply(404, {"error": "unknown clip"})
            elif path == "/observation":
                with shared.lock:
                    observation = shared.observation
                self.reply(
                    200 if observation else 503,
                    observation or {"error": "robot camera unavailable"},
                )
            elif path == "/robot-frame.jpg":
                with shared.lock:
                    frame = shared.robot_frame
                self.reply(200 if frame else 503, frame or b"", "image/jpeg")
            elif path == "/scenes":
                self.reply(200, shared.catalog.public())
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
            path = urlsplit(self.path).path
            if path in {"/say", "/speech/played", "/transcribe", "/transcribe-local"}:
                self.speech_request(path)
                return
            if path != "/control":
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
                command = validate_control(
                    json.loads(self.rfile.read(length)), shared.catalog.entries
                )
                if command.get("preset") == "room":
                    with shared.lock:
                        current = shared.state.get("current_scene")
                    if not current or not current.get("overview"):
                        raise ValueError("room camera requires a scene overview")
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

        def speech_request(self, path):
            try:
                limit = 5600000 if path in ('/transcribe', '/transcribe-local') else 10000
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= limit or self.headers.get_content_type() != "application/json":
                    raise ValueError()
                self.connection.settimeout(5)
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise TypeError()
                if path == '/transcribe-local':
                    if set(value) != {'audio_b64', 'format', 'source', 'utterance_id'} or value['format'] != 'wav' or value['source'] != 'simulation_audio':
                        raise ValueError()
                    uid = str(UUID(value['utterance_id']))
                    raw = base64.b64decode(value['audio_b64'], validate=True)
                    if not shared.stt_lock.acquire(blocking=False):
                        self.reply(429, {'error': 'Local transcription is busy'})
                        return
                    try:
                        from robot.simulation.local_stt import LocalSTTAdapter, LocalSTTError
                        from dataclasses import asdict
                        if shared.stt is None:
                            shared.stt = LocalSTTAdapter()
                        result = asdict(shared.stt.transcribe(raw, utterance_id=uid))
                        result.update(source='simulation_audio', provider={'mode': 'local', 'model': result['model']},
                                      quality='Raw Whisper signals; not calibrated confidence')
                        self.reply(200, result)
                    except LocalSTTError:
                        self.reply(422, {'error': 'Invalid or unrecognized local audio'})
                    finally:
                        shared.stt_lock.release()
                    return
                if path == "/transcribe":
                    import os
                    import urllib.error
                    import urllib.request
                    headers = {"Content-Type": "application/json"}
                    token = os.getenv("ANNIE_BRAIN_TOKEN", os.getenv("ANNIE_API_TOKEN", ""))
                    if token:
                        headers["Authorization"] = "Bearer " + token
                    req = urllib.request.Request("http://127.0.0.1:8002/transcribe", data=json.dumps(value).encode(), headers=headers)
                    try:
                        with urllib.request.urlopen(req, timeout=35) as response:
                            payload = response.read(64001)
                            if len(payload) > 64000:
                                raise ValueError()
                            self.reply(200, json.loads(payload))
                    except urllib.error.HTTPError as exc:
                        self.reply(exc.code, {"error": "Audio inference request failed"})
                    except (urllib.error.URLError, TimeoutError):
                        self.reply(503, {"error": "Audio brain unavailable"})
                    return
                if path == "/speech/played":
                    if set(value) != {"command_id"}:
                        raise ValueError()
                    cid = str(UUID(value["command_id"]))
                    with shared.lock:
                        if cid not in shared.speech:
                            raise ValueError()
                        shared.speech[cid]["status"] = "played"
                        shared.speech[cid]["played"] = True
                    self.reply(200, {"command_id": cid, "status": "played"})
                    return
                if not set(value) <= {"text", "command_id"}:
                    raise ValueError()
                text = value.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise ValueError()
                cid = str(UUID(value["command_id"])) if "command_id" in value else str(uuid4())
                if not shared.speech_lock.acquire(blocking=False):
                    self.reply(429, {"error": "Speech synthesis is busy"})
                    return
                try:
                    with shared.lock:
                        clip = shared.speech.get(cid)
                    if clip is None:
                        try:
                            from robot.simulation.speech import SpeechAdapter
                        except ModuleNotFoundError:
                            from speech import SpeechAdapter
                        clip = asyncio.run(SpeechAdapter().speak(text, cid))
                        clip.update(text=text, url=f"/speech/{cid}.wav")
                        with shared.lock:
                            shared.speech[cid] = clip
                            while len(shared.speech) > 10:
                                shared.speech.pop(next(iter(shared.speech)))
                    self.reply(202, {k: v for k, v in clip.items() if k != "file_path"})
                finally:
                    shared.speech_lock.release()
            except (ValueError, TypeError, OSError, KeyError):
                self.reply(422, {"error": "Invalid speech request"})
            except Exception:  # noqa: BLE001 - never expose speech subprocess details
                self.reply(503, {"error": "Speech synthesis unavailable"})

    return Handler


class SceneCatalog:
    """Load trusted local metadata; expose no model paths through HTTP."""

    def __init__(self, path=None):
        self.entries = {}
        self.paths = {}
        self.version, self.seed = 1, None
        if path is None:
            return
        path = path.resolve()
        manifest = json.loads(path.read_text())
        self.version, self.seed = manifest.get("version", 1), manifest.get("seed")
        for item in manifest["scenes"]:
            scene_id = item["id"]
            if (
                not isinstance(scene_id, str)
                or not scene_id
                or scene_id in self.entries
            ):
                raise ValueError("scene IDs must be unique nonempty strings")
            target = (path.parent / item["file"]).resolve()
            if not target.is_relative_to(path.parent) or target.suffix != ".xml":
                raise ValueError(
                    "catalog model must be an XML inside the scene directory"
                )
            entry = {
                key: item[key]
                for key in (
                    "id",
                    "title",
                    "description",
                    "category",
                    "seed",
                    "ground_truth",
                    "overview",
                    "timeline",
                    "scenario_duration_s",
                )
                if key in item
            }
            entry["ground_truth_source"] = "authored_scene_metadata; not perception"
            overview = entry.get("overview")
            if overview is not None:
                values = list(overview["lookat"]) + [
                    overview[key] for key in ("distance", "azimuth", "elevation")
                ]
                if (
                    len(overview["lookat"]) != 3
                    or not all(
                        isinstance(v, (int, float))
                        and not isinstance(v, bool)
                        and math.isfinite(v)
                        for v in values
                    )
                    or not 0.4 <= overview["distance"] <= 100
                ):
                    raise ValueError("invalid scene overview camera")
            json.dumps(entry, allow_nan=False)
            self.entries[scene_id], self.paths[scene_id] = entry, target

    def public(self):
        return {
            "version": self.version,
            "seed": self.seed,
            "count": len(self.entries),
            "scenes": list(self.entries.values()),
        }


class MujocoSession:
    """A model, renderer and controls, all created and used on the main thread."""

    def __init__(self, path, scene=None, locomotion=False, person_safety=None):
        import mujoco

        self.mj = mujoco
        self.locomotion_enabled = locomotion
        self.controller = self.navigator = None
        self.person_safety = person_safety
        if locomotion:
            try:
                from robot.simulation.locomotion import prepare_locomotion_model
            except ModuleNotFoundError:
                from locomotion import prepare_locomotion_model
            path = prepare_locomotion_model(path)
        self.path, self.scene = path, scene
        self.model = model = mujoco.MjModel.from_xml_path(str(path.resolve()))
        if not math.isfinite(float(model.opt.timestep)) or model.opt.timestep <= 0:
            raise ValueError("model timestep must be finite and positive")
        self.data = mujoco.MjData(model)
        self.camera = mujoco.MjvCamera()
        mujoco.mjv_defaultCamera(self.camera)
        self.track_robot = True
        self.visual_options = mujoco.MjvOption()
        self.visual_options.geomgroup[3] = (
            0  # Hide Go2 collision proxies; keep visual meshes.
        )
        self.camera.azimuth, self.camera.elevation, self.camera.distance = PRESETS[
            "side"
        ]
        self.running, self.steps, self.accumulator = False, 0, 0.0
        self.physics_error = self.render_error = None
        self.hold = bool(scene)  # Furnished scenarios start with powered joint holding.
        self.requested_speed = 1.0
        self.position_servos = bool(model.nu) and all(model.actuator_biasprm[:, 1] < 0)
        self.joints = joints = model.actuator_trnid[:, 0]
        self.hold_supported = (
            bool(model.nu)
            and not self.position_servos
            and all(
                model.actuator_trntype[i] == mujoco.mjtTrn.mjTRN_JOINT
                and model.jnt_type[joints[i]] == mujoco.mjtJoint.mjJNT_HINGE
                and model.actuator_gear[i, 0] == 1
                for i in range(model.nu)
            )
        )
        self.reset()
        self.nominal_ctrl = self.data.ctrl.copy()
        self.targets = (
            self.data.qpos[model.jnt_qposadr[joints]].copy()
            if self.hold_supported
            else None
        )
        if locomotion:
            try:
                from robot.simulation.locomotion import LocomotionController
                from robot.simulation.navigation import Navigator
            except ModuleNotFoundError:
                from locomotion import LocomotionController
                from navigation import Navigator
            self.controller = LocomotionController(model, self.data)
            self.navigator = Navigator(model, self.data, scene)
        self.controls()
        if scene and scene.get("overview"):
            self.set_camera({"preset": "room"})
        self.renderer = mujoco.Renderer(model, height=480, width=640)

    def close(self):
        self.renderer.close()

    def reset(self):
        if self.model.nkey:
            self.mj.mj_resetDataKeyframe(self.model, self.data, 0)
        else:
            self.mj.mj_resetData(self.model, self.data)
        self.mj.mj_forward(self.model, self.data)
        self.physics_error = physics_fault(self.data, self.mj)
        self.map_id = "sim-" + str(uuid4())
        if self.person_safety:
            self.person_safety.reset(self.map_id)
        if self.controller:
            self.controller.reset()
        if self.navigator:
            self.navigator = type(self.navigator)(self.model, self.data, self.scene)
        self.running, self.steps, self.accumulator = False, 0, 0.0
        self.active_wall_time = self.dropped_wall_seconds = 0.0
        self.paced_sim_time = self.speed_wall_baseline = self.speed_sim_baseline = 0.0

    def controls(self):
        model, data = self.model, self.data
        if self.controller:
            safety = self.person_safety.snapshot() if self.person_safety else None
            if safety and safety['blocked']:
                if self.navigator.state in ('moving', 'scanning', 'turning'):
                    self.navigator.fail(safety['reason'])
                self.controller.apply(0, 0, 0)
            else:
                self.controller.apply(*self.navigator.velocity())
        elif self.position_servos:
            data.ctrl[:] = self.nominal_ctrl
        elif self.hold and self.hold_supported:
            torque = (
                40 * (self.targets - data.qpos[model.jnt_qposadr[self.joints]])
                - 2 * data.qvel[model.jnt_dofadr[self.joints]]
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

    def step(self):
        if self.physics_error:
            return
        try:
            self.controls()
            self.mj.mj_step(self.model, self.data)
            self.steps += 1
            self.physics_error = physics_fault(self.data, self.mj)
        except Exception as exc:  # noqa: BLE001
            self.physics_error = (
                f"{type(exc).__name__}: physics step failed; reset required"
            )
        if self.physics_error:
            self.running, self.accumulator = False, 0.0

    def set_speed(self, value):
        self.requested_speed = float(value)
        self.speed_wall_baseline = self.active_wall_time
        self.speed_sim_baseline = self.paced_sim_time

    def advance(self, elapsed):
        if not self.running or not math.isfinite(elapsed) or elapsed <= 0:
            return
        self.active_wall_time += elapsed
        self.accumulator += elapsed * self.requested_speed
        # Bound catch-up to half a wall second; report discarded time explicitly.
        maximum_debt = 0.5 * self.requested_speed
        if self.accumulator > maximum_debt:
            self.dropped_wall_seconds += (
                self.accumulator - maximum_debt
            ) / self.requested_speed
            self.accumulator = maximum_debt
        before = float(self.data.time)
        for _ in range(
            min(int((self.accumulator + 1e-12) / self.model.opt.timestep), 250)
        ):
            self.step()
            if self.physics_error:
                break
            self.accumulator = max(0.0, self.accumulator - self.model.opt.timestep)
            duration = self.scene.get("scenario_duration_s") if self.scene else None
            if (
                not self.controller
                and isinstance(duration, (int, float))
                and duration > 0
                and self.data.time >= duration
            ):
                self.running = False
                self.accumulator = 0.0
                break
        if not self.physics_error:
            self.paced_sim_time += max(0.0, float(self.data.time) - before)

    def set_camera(self, command):
        if command.get("preset") == "room":
            if not self.scene or not self.scene.get("overview"):
                return
            overview = self.scene["overview"]
            self.camera.lookat[:] = overview["lookat"]
            for name in ("azimuth", "elevation", "distance"):
                setattr(self.camera, name, overview[name])
            self.track_robot = False
        elif "preset" in command:
            self.camera.azimuth, self.camera.elevation, self.camera.distance = PRESETS[
                command["preset"]
            ]
            self.track_robot = True
        else:
            for name in ("azimuth", "elevation", "distance"):
                if name in command:
                    setattr(self.camera, name, command[name])

    def render(self):
        from PIL import Image

        if self.track_robot:
            self.camera.lookat[:] = (
                self.data.qpos[:3] if self.model.nq >= 7 else self.model.stat.center
            )
        self.renderer.update_scene(
            self.data, camera=self.camera, scene_option=self.visual_options
        )
        output = io.BytesIO()
        Image.fromarray(self.renderer.render()).save(output, format="JPEG", quality=85)
        self.render_error = None
        return output.getvalue()

    def observation(self):
        from PIL import Image

        if not self.controller:
            return None, None
        self.renderer.update_scene(
            self.data, camera="robot_front", scene_option=self.visual_options
        )
        output = io.BytesIO()
        Image.fromarray(self.renderer.render()).save(output, format="JPEG", quality=85)
        jpeg = output.getvalue()
        w, x, y, z = self.data.qpos[3:7]
        pose = {
            "x": float(self.data.qpos[0]),
            "y": float(self.data.qpos[1]),
            "yaw": math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
            "map_id": self.map_id,
        }
        return jpeg, {
            "frame_id": str(uuid4()),
            "ts": int(time.time() * 1000),
            "pose": pose,
            "source": "simulation_render",
            "jpeg_b64": base64.b64encode(jpeg).decode(),
            "simulation_time": float(self.data.time),
            "camera_id": "robot_front",
        }

    def state(self):
        model, data = self.model, self.data
        simulation_time = finite_number(data.time)
        day_seconds = (9 * 3600 + int(simulation_time or 0)) % 86400
        active_since_speed = self.active_wall_time - self.speed_wall_baseline
        achieved = (
            (self.paced_sim_time - self.speed_sim_baseline) / active_since_speed
            if active_since_speed > 0
            else None
        )
        current_phase = None
        timeline = self.scene.get("timeline", []) if self.scene else []
        if isinstance(timeline, list):
            for phase in timeline:
                if (
                    isinstance(phase, dict)
                    and isinstance(phase.get("start_s"), (int, float))
                    and (simulation_time or 0) >= phase["start_s"]
                ):
                    current_phase = phase.get("label", phase.get("name"))
        return {
            "map_id": self.map_id,
            "locomotion": self.controller.state() if self.controller else None,
            "navigation": self.navigator.snapshot() if self.navigator else None,
            "person_safety": self.person_safety.snapshot() if self.person_safety else {'enabled': False},
            "simulation_time": simulation_time,
            "scene_elapsed": simulation_time,
            "active_wall_time": self.active_wall_time,
            "requested_speed": self.requested_speed,
            "achieved_speed": finite_number(achieved) if achieved is not None else None,
            "achieved_speed_basis": "active time since speed change or reset; excludes manual steps",
            "dropped_wall_seconds": self.dropped_wall_seconds,
            "time_of_day": f"{day_seconds // 3600:02}:{day_seconds // 60 % 60:02}:{day_seconds % 60:02}",
            "current_phase": current_phase,
            "running": self.running,
            "steps": self.steps,
            "mujoco_version": self.mj.__version__,
            "modelname": model.names.split(b"\x00", 1)[0].decode(
                "utf-8", errors="replace"
            )
            or self.path.stem,
            "physics_dt": float(model.opt.timestep),
            "qpos_base": [finite_number(v) for v in data.qpos[:7]],
            "nu": model.nu,
            "source": "direct_mujoco",
            "control_mode": "Trained DimOS Go1 walking policy (Go1 simulation surrogate)"
            if self.controller
            else "keyframe joint position targets; no gait controller"
            if self.position_servos
            else "PD joint hold (kp=40, kd=2); no gait controller"
            if self.hold
            else "passive physics (zero motor torque); no gait controller",
            "hold_enabled": self.hold,
            "hold_supported": self.hold_supported and not self.controller,
            "physics_error": self.physics_error,
            "render_error": self.render_error,
            "frame_width": 640,
            "frame_height": 480,
            "scene_id": self.scene["id"] if self.scene else None,
            "current_scene": self.scene,
            "camera_mode": "robot" if self.track_robot else "room",
        }


def run(args):
    catalog = SceneCatalog(args.scenes)
    guard = None
    if args.person_safety:
        from robot.simulation.person_safety import PersonSafety
        guard = PersonSafety()
    first_id = next(iter(catalog.entries), None)
    session = MujocoSession(
        catalog.paths[first_id] if first_id else args.model,
        catalog.entries.get(first_id),
        args.locomotion,
        guard,
    )
    shared = Shared()
    def warm_question():
        from robot.simulation.speech import SpeechAdapter, CHECKIN_PROMPT
        try:
            asyncio.run(SpeechAdapter().speak(CHECKIN_PROMPT, str(uuid4())))
        except Exception:
            # A later speech request reports an explicit failure if unavailable.
            pass
    threading.Thread(target=warm_question, daemon=True, name='question-audio-warmup').start()
    shared.catalog = catalog
    server = ThreadingHTTPServer((args.host, args.port), handler_for(shared, args.port))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        f"MuJoCo {session.mj.__version__}: http://{args.host}:{args.port} (paused)",
        flush=True,
    )
    previous, next_frame, next_observation = time.monotonic(), 0.0, 0.0
    scene_error = catalog_error = None
    catalog_generation = None
    try:
        while True:
            now = time.monotonic()
            elapsed = max(0.0, now - previous)
            previous = now
            session.advance(elapsed)
            for _ in range(100):
                try:
                    command = shared.commands.get_nowait()
                except queue.Empty:
                    break
                action = command["action"]
                if action in {"scene", "generate"}:
                    candidate = None
                    candidate_catalog = catalog
                    generation = None
                    with shared.lock:
                        shared.state = {
                            **shared.state,
                            "scene_loading": command.get("id", "generating"),
                            "scene_error": None,
                            "catalog_error": None,
                        }
                    try:
                        if action == "generate":
                            try:
                                from robot.simulation.scenes import generate_batch
                            except ModuleNotFoundError:
                                from scenes import generate_batch
                            nonce = str(time.time_ns())
                            output = args.factory_output / ("batch-" + nonce)
                            generate_batch(
                                assets=args.factory_assets,
                                output=output,
                                count=command["count"],
                                seed=command["seed"],
                            )
                            candidate_catalog = SceneCatalog(output / "manifest.json")
                            scene_id = next(iter(candidate_catalog.entries))
                            generation = {
                                "seed": command["seed"],
                                "count": len(candidate_catalog.entries),
                                "nonce": nonce,
                            }
                        else:
                            scene_id = command["id"]
                        candidate = MujocoSession(
                            candidate_catalog.paths[scene_id],
                            candidate_catalog.entries[scene_id],
                            args.locomotion,
                            guard,
                        )
                        if candidate.physics_error:
                            raise ValueError("scene has invalid initial physics")
                        frame = candidate.render()
                    except Exception as exc:  # noqa: BLE001 - retain working world on load failure
                        if candidate is not None:
                            candidate.close()
                        message = f"{type(exc).__name__}: scene load failed; previous scene retained"
                        if action == "generate":
                            catalog_error = message
                        else:
                            scene_error = message
                    else:
                        old, session = session, candidate
                        catalog = candidate_catalog
                        if generation:
                            catalog_generation = generation
                        with shared.lock:
                            shared.catalog = catalog
                            shared.frame = frame
                            shared.observation = shared.robot_frame = None
                            shared.state = {
                                **session.state(),
                                "ready": True,
                                "scene_count": len(catalog.entries),
                                "scene_loading": None,
                                "scene_error": None,
                                "catalog_generation": catalog_generation,
                                "catalog_error": None,
                            }
                        old.close()
                        scene_error = catalog_error = None
                    previous = time.monotonic()
                    elapsed = 0.0
                elif action in {"play", "pause"}:
                    session.running = action == "play" and session.physics_error is None
                    session.accumulator = 0.0
                elif action == "mission":
                    if session.navigator:
                        if guard and command['cmd'] not in ('stop',):
                            guard.explicit_restart()
                        session.navigator.command(
                            command["cmd"],
                            command.get("waypoint"),
                            command.get("command_id"),
                            heading=command.get('heading'),
                        )
                        session.running = session.physics_error is None
                    else:
                        scene_error = "Restart viewer with --locomotion to run missions"
                elif action == "speed":
                    session.set_speed(command["value"])
                elif action == "hold":
                    session.hold = command["enabled"] and session.hold_supported
                elif action == "reset":
                    session.reset()
                    with shared.lock:
                        shared.observation = None
                elif action == "step":
                    session.running = False
                    session.step()
                elif action == "camera":
                    session.set_camera(command)
            if now >= next_frame and session.physics_error is None:
                next_frame = now + 0.1
                try:
                    frame = session.render()
                    with shared.lock:
                        shared.frame = frame
                except Exception as exc:  # noqa: BLE001
                    session.render_error = f"{type(exc).__name__}: rendering failed"
                    session.running = False
            if (
                now >= next_observation
                and session.controller
                and session.physics_error is None
                and (guard or session.running or shared.observation is None)
            ):
                next_observation = now + (0.1 if guard else 0.5)
                try:
                    robot_frame, observation = session.observation()
                    if guard:
                        guard.submit(observation)
                    with shared.lock:
                        shared.robot_frame = robot_frame
                        # Safety preview stays fresh while paused so an explicit
                        # mission can start; incident evidence remains paused.
                        if session.running or shared.observation is None:
                            shared.observation = observation
                except Exception as exc:  # noqa: BLE001 - expose rendering failure
                    session.render_error = f"{type(exc).__name__}: robot camera failed"
            with shared.lock:
                session_state = session.state()
                clips = [{k: v for k, v in item.items() if k != "file_path"} for item in shared.speech.values()]
                session_state["speech"] = clips
                if session_state["navigation"]:
                    session_state["navigation"]["commands"] = session_state["navigation"]["commands"] + [
                        {"command_id": c["command_id"], "status": "completed" if c["status"] == "played" else "executing",
                         "detail": "Browser reported playback finished" if c["status"] == "played" else "Audio synthesized; awaiting browser playback"} for c in clips]
                shared.state = {
                    **session_state,
                    "ready": shared.frame is not None,
                    "scene_count": len(catalog.entries),
                    "scene_loading": None,
                    "scene_error": scene_error,
                    "catalog_generation": catalog_generation,
                    "catalog_error": catalog_error,
                }
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        if guard:
            guard.close()
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument('--person-safety', action='store_true',
                        help='Inhibit movement on person detection or unavailable camera detector')
    parser.add_argument(
        "--locomotion",
        action="store_true",
        help="Use matched Go1 model and trained walking policy",
    )
    parser.add_argument(
        "--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1"
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--scenes",
        type=Path,
        default=None,
        help="Local scene catalog; selects its first scene on startup",
    )
    parser.add_argument(
        "--factory-assets",
        type=Path,
        help="Go2 asset directory; defaults to model parent",
    )
    parser.add_argument(
        "--factory-output", type=Path, default=Path(".data/simulation/scenes")
    )
    args = parser.parse_args()
    args.factory_assets = args.factory_assets or args.model.parent
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    run(args)


if __name__ == "__main__":
    main()
