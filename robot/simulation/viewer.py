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

try:
    from robot.simulation.native_audio import NativeAudioError, NativeAudioPlayer
except ModuleNotFoundError:  # direct execution from the simulation directory
    from native_audio import NativeAudioError, NativeAudioPlayer

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
        "overlays",
        "hold",
        "scene",
        "generate",
        "speed",
        "mission",
        "resident",
        "autonomy",
        "intelligence",
    }:
        raise ValueError("action must be play, pause, reset, step, camera, or hold")
    if value["action"] == "mission":
        if not set(value) <= {"action", "cmd", "waypoint", "command_id", "heading", "trick"} or value.get(
            "cmd"
        ) not in {"goto", "patrol", "stop", "resume", "look", "turn", "trick"}:
            raise ValueError("invalid mission")
        if value['cmd'] == 'trick':
            from robot.simulation.tricks import TRICKS
            if value.get('trick') not in TRICKS:
                raise ValueError('trick must be one of ' + ', '.join(sorted(TRICKS)))
        elif 'trick' in value:
            raise ValueError('trick is only valid for the trick command')
        if value["cmd"] == "goto" and value.get("waypoint") not in {
            "home",
            "living-room",
            "bedroom",
            "hallway",
            "kitchen", "study", "garden-room",
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
    if value["action"] == "resident":
        if set(value) != {"action", "cmd"} or value.get("cmd") not in {"routine", "fall", "recover"}:
            raise ValueError("resident requires routine, fall or recover")
        return value
    if value["action"] == "autonomy":
        if set(value) != {"action", "mode"} or value.get("mode") not in {"patrol", "watch", "paused", "find_resident"}:
            raise ValueError("invalid autonomy mode")
        return value
    if value['action']=='intelligence':
        if (not {'action','enabled','goal'} <= set(value)
                or not set(value) <= {'action','enabled','goal','require_speech'}
                or type(value['enabled']) is not bool or not isinstance(value['goal'],str)
                or not 1<=len(value['goal'].strip())<=500
                or ('require_speech' in value and type(value['require_speech']) is not bool)):
            raise ValueError('intelligence requires enabled and a goal up to 500 characters')
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
    if value["action"] == "overlays":
        if not 2 <= len(value) <= 4 or not set(value) <= {"action", "lidar", "trajectory", "route"}:
            raise ValueError("overlays requires lidar, trajectory, or route")
        if any(type(enabled) is not bool for key, enabled in value.items() if key != "action"):
            raise ValueError("overlay values must be boolean")
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
            "downstairs", "upstairs", "whole-house",
        }:
            raise ValueError("preset must be front, side, or top")
    else:
        if (
            not set(value) <= {"action", "azimuth", "elevation", "distance", "lookat"}
            or len(value) < 2
        ):
            raise ValueError("camera requires preset or bounded camera coordinates")
        if "lookat" in value:
            point = value["lookat"]
            if not isinstance(point, list) or len(point) != 3 or any(
                type(coordinate) not in (int, float) or not math.isfinite(coordinate)
                or not -100 <= coordinate <= 100 for coordinate in point
            ):
                raise ValueError("lookat requires three finite coordinates in [-100, 100] metres")
        for name, bounds in {
            "azimuth": (-360, 360),
            "elevation": (-90, 90),
            "distance": (0.4, 40),
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
        self.native_player = None
        self.resident_reply = None
        self.demo_process = None
        self.agent_process = None
        self.agent_max_inferences = 20
        self.demo_brain_url = "http://127.0.0.1:8004"
        self.demo_allow_cloud = False


def playback_callback_for(shared, command_id):
    """Record native playback process states; never human audibility."""

    def on_state(payload):
        state = payload.get("state")
        with shared.lock:
            entry = shared.speech.get(command_id)
            if entry is None:
                return
            wall = payload.get("wall")
            entry["playback_updated_wall"] = wall
            if state == "playing":
                entry["status"] = "playing"
                entry["playback_started_wall"] = wall
            elif state == "played":
                entry["status"] = "played"
                entry["played"] = True
                entry["playback_ended_wall"] = wall
                # Reached only after the afplay process exited 0.
                entry["playback_detail"] = "playback_process_completed"
            elif state == "failed":
                entry["status"] = "failed"
                entry["playback_ended_wall"] = wall
                entry["playback_detail"] = "playback_process_failed"
                entry["playback_error"] = str(
                    payload.get("error") or "unknown playback failure"
                )

    return on_state


def speech_receipts(clips, native):
    """Map speech clip statuses to navigation command receipts."""

    receipts = []
    for clip in clips:
        if clip["status"] == "played":
            receipts.append(
                {
                    "command_id": clip["command_id"],
                    "status": "completed",
                    "detail": "playback_process_completed"
                    if native
                    else "Browser reported playback finished",
                }
            )
        elif clip["status"] == "failed":
            receipts.append(
                {
                    "command_id": clip["command_id"],
                    "status": "failed",
                    "detail": clip.get("playback_error")
                    if native
                    else "Playback failed",
                }
            )
        else:
            receipts.append(
                {
                    "command_id": clip["command_id"],
                    "status": "executing",
                    "detail": "Audio synthesized; awaiting native playback"
                    if native
                    else "Audio synthesized; awaiting browser playback",
                }
            )
    return receipts


def active_checkin():
    import os
    import httpx
    token=os.getenv('ANNIE_API_TOKEN')
    headers={'Authorization':'Bearer '+token} if token else {}
    with httpx.Client(base_url='http://127.0.0.1:8000',headers=headers,
                      timeout=2,trust_env=False) as app:
        response=app.get('/status')
        response.raise_for_status()
        return bool(response.json().get('pending_checkin'))


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
                state['audio_output'] = 'native' if shared.native_player else 'browser'
                self.reply(200, state)
            elif path == '/checkin-state':
                try:
                    import os
                    import httpx
                    token = os.getenv('ANNIE_API_TOKEN')
                    headers = {'Authorization': 'Bearer ' + token} if token else {}
                    with httpx.Client(base_url='http://127.0.0.1:8000', headers=headers,
                                      timeout=2, trust_env=False) as app:
                        response = app.get('/status')
                        response.raise_for_status()
                        pending = response.json().get('pending_checkin')
                    with shared.lock:
                        last_reply = shared.resident_reply
                    self.reply(200, {'pending_checkin': pending, 'last_reply': last_reply})
                except Exception:
                    self.reply(503, {'error': 'Check-in API unavailable'})
            elif path == "/brain-state":
                try:
                    status_file = Path(".data/simulation/bridge-status.json")
                    if status_file.stat().st_size > 32000:
                        raise ValueError()
                    self.reply(200, json.loads(status_file.read_text()))
                except (OSError, ValueError):
                    self.reply(503, {"last_error": "Brain bridge not connected"})
            elif path == '/demo-state':
                try:
                    report = Path('.data/simulation/house-demo.json')
                    if report.stat().st_size > 64000:
                        raise ValueError('Report is too large')
                    data=json.loads(report.read_text())
                    if data.get('status')=='running' and shared.demo_process is not None and shared.demo_process.poll() is not None:
                        data.update(status='failed',error='Demonstration process exited before completing')
                    self.reply(200, data)
                except (OSError, ValueError):
                    self.reply(200, {'status':'not_started','stages':[]})
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
            if path == '/demo/start':
                # Fixed local acceptance driver, no caller-provided executable,
                # model, paths or shell arguments. Its inputs are synthetic.
                import subprocess
                try:
                    pending=active_checkin()
                except Exception:
                    self.reply(503, {'error':'Check-in status unavailable; demo was not started'})
                    return
                if pending:
                    self.reply(409, {'error':'Resolve the active check-in before starting a new demonstration'})
                    return
                with shared.lock:
                    if shared.demo_process and shared.demo_process.poll() is None:
                        self.reply(409, {'error':'A full-house demonstration is already running'})
                        return
                    if shared.agent_process and shared.agent_process.poll() is None:
                        shared.agent_process.terminate()
                        shared.agent_process.wait(timeout=5)
                    from robot.simulation.bridge_lease import body_bridge_lease
                    try:
                        with body_bridge_lease(): pass
                    except RuntimeError as exc:
                        self.reply(409, {'error':str(exc)})
                        return
                    root=Path(__file__).resolve().parents[2]
                    shared.demo_process=subprocess.Popen([
                        str(root/'.venv/bin/python'),'-m','robot.simulation.house_demo',
                        '--brain-url', shared.demo_brain_url,
                        *(['--allow-cloud'] if shared.demo_allow_cloud else [])],
                        cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                self.reply(202, {'status':'starting'})
                return
            if path in {"/say", "/speech/played", "/transcribe", "/transcribe-local", "/resident-reply"}:
                self.speech_request(path)
                return
            if path not in {"/control", "/agent/start"}:
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
                if path == '/agent/start':
                    if command.get('action')!='intelligence' or command.get('enabled') is not True:
                        raise ValueError('AI start requires an enabled intelligence goal')
                    import subprocess
                    with shared.lock:
                        if shared.demo_process and shared.demo_process.poll() is None:
                            self.reply(409, {'error':'The full-house demonstration currently owns the robot goal'})
                            return
                        if not shared.agent_process or shared.agent_process.poll() is not None:
                            from robot.simulation.bridge_lease import body_bridge_lease
                            try:
                                with body_bridge_lease(): pass
                            except RuntimeError as exc:
                                self.reply(409, {'error':str(exc)})
                                return
                            root=Path(__file__).resolve().parents[2]
                            shared.agent_process=subprocess.Popen([
                                str(root/'.venv/bin/python'),'-m','robot.simulation.bridge',
                                '--perception','agent','--brain-url',shared.demo_brain_url,
                                '--max-inferences',str(shared.agent_max_inferences),
                                *([] if shared.demo_allow_cloud else ['--continuous-local'])],
                                cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

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
                if path == '/resident-reply':
                    from robot.simulation.resident_reply import KINDS, ReplyError, replay
                    import httpx
                    import os
                    if set(value) != {'fixture', 'event_id'} or value['fixture'] not in KINDS:
                        raise ValueError()
                    event_id = str(UUID(value['event_id']))
                    token = os.getenv('ANNIE_API_TOKEN')
                    headers = {'Authorization': 'Bearer ' + token} if token else {}
                    try:
                        with httpx.Client(base_url='http://127.0.0.1:8000', headers=headers,
                                          timeout=5, trust_env=False) as app, \
                                httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=15,
                                             trust_env=False) as viewer:
                            result = replay(value['fixture'], event_id=event_id, app=app, viewer=viewer)
                        with shared.lock:
                            shared.resident_reply = result
                        self.reply(200, result)
                    except ReplyError as exc:
                        self.reply(409, {'error': str(exc)})
                    return
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
                        clip = shared.speech.get(cid)
                        if clip is None:
                            raise ValueError()
                        if clip.get("playback") == "native":
                            self.reply(
                                409,
                                {"error": "This clip plays on the native output; browser receipts are not accepted"},
                            )
                            return
                        clip["status"] = "played"
                        clip["played"] = True
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
                        native_mode = shared.native_player is not None
                        if native_mode:
                            clip["playback"] = "native"
                            clip["output"] = "macos_default_output"
                        with shared.lock:
                            shared.speech[cid] = clip
                            while len(shared.speech) > 10:
                                shared.speech.pop(next(iter(shared.speech)))
                        if native_mode:
                            # Single enqueue per clip, on first synthesis only.
                            try:
                                shared.native_player.enqueue(
                                    {
                                        "file_path": str(Path(clip["file_path"]).resolve()),
                                        "duration_s": clip["duration_s"],
                                    },
                                    on_state_callback=playback_callback_for(
                                        shared, cid
                                    ),
                                )
                            except NativeAudioError as exc:
                                with shared.lock:
                                    entry = shared.speech.get(cid)
                                    if entry is not None:
                                        entry["status"] = "failed"
                                        entry["playback_detail"] = (
                                            "playback_process_failed"
                                        )
                                        entry["playback_error"] = str(exc)
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
                    "rooms", "floors", "stairs", "waypoints", "daily_life",
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

    def __init__(self, path, scene=None, locomotion=False, person_safety=None, person_policy='stop'):
        import mujoco

        self.mj = mujoco
        self.locomotion_enabled = locomotion
        self.controller = self.navigator = None
        self.resident = None
        self.resident_guard = {'blocked': False, 'source': 'authored_simulator_proximity'}
        self.autonomy_mode, self.autonomy_revision = 'paused', 0
        self.intelligence_enabled, self.intelligence_revision = False, 0
        self.intelligence_require_speech = False
        self.intelligence_goal = 'Explore the ground floor and check on the resident. Describe what you see, stay clear of people, and choose your next action from camera evidence.'
        self.person_safety = person_safety
        self.person_policy = person_policy
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
        self.observation_options = mujoco.MjvOption()
        self.observation_options.geomgroup[3] = 0
        if scene and scene.get('floors'):
            self.visual_options.geomgroup[4] = 0
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
        if scene and scene.get('daily_life'):
            from robot.simulation.daily_life import ResidentRoutine
            self.resident = ResidentRoutine(model, self.data, scene)
            mujoco.mj_forward(model, self.data)
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
        from robot.simulation.spatial import SpatialSensor
        self.spatial = SpatialSensor(model)
        self.spatial_state = {}
        self.spatial_error = None
        self.overlays = {"lidar": True, "trajectory": True, "route": True}

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
        if getattr(self, "spatial", None):
            self.spatial.reset()
            self.spatial_state = {}
        if self.person_safety:
            self.person_safety.reset(self.map_id)
        if self.controller:
            self.controller.reset()
        if self.resident:
            self.resident.reset()
            self.mj.mj_forward(self.model,self.data)
        self.autonomy_mode = 'paused'
        self.autonomy_revision += 1
        self.intelligence_enabled = False
        self.intelligence_require_speech = False
        self.intelligence_revision += 1
        if self.navigator:
            self.navigator = type(self.navigator)(self.model, self.data, self.scene)
        self.running, self.steps, self.accumulator = False, 0, 0.0
        self.active_wall_time = self.dropped_wall_seconds = 0.0
        self.paced_sim_time = self.speed_wall_baseline = self.speed_sim_baseline = 0.0

    def controls(self):
        model, data = self.model, self.data
        if self.controller:
            safety = self.person_safety.snapshot() if self.person_safety else None
            if self.resident:
                positions=self.data.mocap_pos
                near = min(math.hypot(p[0]-data.qpos[0],p[1]-data.qpos[1]) for p in positions if p[2]<2) if len(positions) else math.inf
                self.resident_guard = {'blocked':near<.85, 'distance_m':near, 'source':'authored_simulator_proximity'}
            enforce_person=getattr(self,'person_policy','stop')=='stop'
            self.resident_guard['enforced']=enforce_person
            blocked = ((safety and (not safety['ready'] or (enforce_person and safety['blocked'])))
                       or (enforce_person and self.resident_guard['blocked']))
            if blocked:
                if self.navigator.state in ('moving', 'scanning', 'turning'):
                    self.navigator.fail(safety['reason'] if safety and safety['blocked'] else 'Animated resident proximity guard')
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
            if self.resident:
                self.resident.update(float(self.data.time))
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
        if command.get("preset") in {"room", "downstairs", "upstairs", "whole-house"}:
            if not self.scene or not self.scene.get("overview"):
                return
            overview = self.scene["overview"]
            self.camera.lookat[:] = overview["lookat"]
            for name in ("azimuth", "elevation", "distance"):
                setattr(self.camera, name, overview[name])
            self.track_robot = False
            preset = command.get('preset')
            if self.scene.get('floors'):
                self.visual_options.geomgroup[4] = int(preset in ('upstairs', 'whole-house'))
                self.camera.lookat[2] = 3.5 if preset == 'upstairs' else 1.2
        elif "preset" in command:
            self.camera.azimuth, self.camera.elevation, self.camera.distance = PRESETS[
                command["preset"]
            ]
            self.track_robot = True
        else:
            for name in ("azimuth", "elevation", "distance"):
                if name in command:
                    setattr(self.camera, name, command[name])
            if "lookat" in command:
                self.camera.lookat[:] = command["lookat"]
                self.track_robot = False

    def render(self):
        from PIL import Image

        if self.track_robot:
            self.camera.lookat[:] = (
                self.data.qpos[:3] if self.model.nq >= 7 else self.model.stat.center
            )
        self.renderer.update_scene(
            self.data, camera=self.camera, scene_option=self.visual_options
        )
        # Operator-only overlays: the robot_front observation is rendered separately
        # and never includes these diagnostic points or planned/measured paths.
        try:
            if self.spatial.update(self.data):
                self.spatial_state = self.spatial.snapshot()
            self.spatial.draw(
                self.renderer.scene,
                lidar=self.overlays["lidar"],
                trajectory=self.overlays["trajectory"],
                route=self.navigator.path if self.navigator and self.overlays["route"] else (),
            )
            self.spatial_error = None
        except Exception as exc:
            self.spatial_error = f"{type(exc).__name__}: spatial overlay unavailable"
        output = io.BytesIO()
        Image.fromarray(self.renderer.render()).save(output, format="JPEG", quality=85)
        self.render_error = None
        return output.getvalue()

    def observation(self):
        from PIL import Image

        if not self.controller:
            return None, None
        self.renderer.update_scene(
            self.data, camera="robot_front", scene_option=self.observation_options
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
            "person_safety": {**self.person_safety.snapshot(), 'enforced':self.person_policy=='stop',
                              'policy':self.person_policy} if self.person_safety else {'enabled': False},
            "resident": self.resident.state() if self.resident else None,
            "resident_guard": self.resident_guard,
            "autonomy_mode": self.autonomy_mode,
            "autonomy_revision": self.autonomy_revision,
            "intelligence_enabled": self.intelligence_enabled,
            "intelligence_goal": self.intelligence_goal,
            "intelligence_revision": self.intelligence_revision,
            "intelligence_require_speech": self.intelligence_require_speech,
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
            "camera": {
                "azimuth": finite_number(self.camera.azimuth),
                "elevation": finite_number(self.camera.elevation),
                "distance": finite_number(self.camera.distance),
                "lookat": [finite_number(value) for value in self.camera.lookat],
            },
            "spatial": {**self.spatial_state, "error": self.spatial_error},
            "overlays": dict(self.overlays),
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
        args.person_policy,
    )
    shared = Shared()
    shared.demo_brain_url = args.demo_brain_url
    shared.demo_allow_cloud = args.demo_allow_cloud
    shared.agent_max_inferences = args.agent_max_inferences
    if args.native_audio:
        shared.native_player = NativeAudioPlayer()
        shared.native_player.start()
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
                            from robot.simulation.house import write_house
                            write_house(args.factory_assets,output)
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
                            args.person_policy,
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
                elif action == 'resident':
                    if session.resident:
                        session.resident.trigger(command['cmd'])
                        session.running = session.physics_error is None
                elif action == 'autonomy':
                    session.autonomy_mode = command['mode']
                    session.autonomy_revision += 1
                    if command['mode'] in ('paused','watch') and session.navigator:
                        session.navigator.command('stop')
                    elif guard:
                        guard.explicit_restart()
                    session.running = session.physics_error is None
                elif action == 'intelligence':
                    session.intelligence_enabled = command['enabled']
                    session.intelligence_goal = command['goal'].strip()
                    session.intelligence_require_speech = command.get('require_speech', False)
                    session.intelligence_revision += 1
                    session.autonomy_mode='paused'
                    if not command['enabled'] and session.navigator:
                        session.navigator.command('stop')
                    elif guard:
                        guard.explicit_restart()
                    session.running=session.physics_error is None
                elif action == "mission":
                    if session.navigator:
                        if guard and command['cmd'] not in ('stop',):
                            guard.explicit_restart()
                        session.navigator.command(
                            command["cmd"],
                            command.get("waypoint"),
                            command.get("command_id"),
                            heading=command.get('heading'),
                            trick=command.get('trick'),
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
                elif action == "overlays":
                    session.overlays.update({key: value for key, value in command.items() if key != "action"})
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
                        *speech_receipts(clips, shared.native_player is not None)]
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
        for child in (shared.agent_process, shared.demo_process):
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        server.shutdown()
        server.server_close()
        if shared.native_player is not None:
            shared.native_player.stop()
        if guard:
            guard.close()
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument('--person-policy', choices=['stop','advisory'], default='stop',
                        help='Simulated person detections stop movement or advise the model')
    parser.add_argument('--demo-brain-url', default='http://127.0.0.1:8004')
    parser.add_argument('--agent-max-inferences', type=int, default=20,
                        help='Bound a cloud AI session; provider lifetime and dollar caps still apply')
    parser.add_argument('--demo-allow-cloud', action='store_true',
                        help='Explicitly allow bounded synthetic demo inference using the configured cloud brain')
    parser.add_argument('--person-safety', action='store_true',
                        help='Inhibit movement on person detection or unavailable camera detector')
    parser.add_argument(
        "--locomotion",
        action="store_true",
        help="Use matched Go1 model and trained walking policy",
    )
    parser.add_argument(
        "--native-audio",
        action="store_true",
        help="Play speech via the native audio player on the macOS default output; "
        "browser playback stays the default when this flag is absent (no fallback)",
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
    if not 1 <= args.agent_max_inferences <= 1000:
        parser.error('--agent-max-inferences must be between 1 and 1000')
    args.factory_assets = args.factory_assets or args.model.parent
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    run(args)


if __name__ == "__main__":
    main()
