#!/usr/bin/env python3
"""Patrol and greet: the physical Go2 walks a slow loop and says hi to people it meets.

Pieces that already ran on this dog tonight, composed: the circle walk (Move at
10 Hz), the keypoint person tracker on the live camera, the firmware Hello
trick, and speech through the host speaker. The policy is deterministic and
unit-tested (`GreetPolicy`): nobody visible -> keep patrolling; a new, close
enough, upright person -> stop, Hello, speak a greeting, then resume; the same
person is not greeted again for the cooldown; a lying person triggers a check-in
question instead of a greeting. Names come from the optional face index when
enrolled; otherwise everyone is "there".

Patrol motion comes from `go2_smart_patrol.PatrolPlanner`: a sweeping wander
that slows and turns away from obstacles seen in the robot's LiDAR voxel map,
backs off and turns after an odometry stall (the collision detector for things
the LiDAR misses), and homes back toward the start point when past the leash.

Supervised commissioning tool: cleared area, operator nearby, 40% battery
floor, stale-telemetry and boundary stops, priority StopMove on every exit.
Software stop only; the host cannot stop the robot over a lost link.

Run from the repository root:
  .cache/dimos/.venv/bin/python robot/go2_patrol_greet.py --ip 172.20.10.10 --duration 300
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repository root
from robot.dog.link.follow import follow_command  # noqa: E402
from robot.dog.planning.brain import SightingMemory, VisionBrain  # noqa: E402
from robot.dog.perception.pipeline import Diag, Perception  # noqa: E402
from robot.dog.voice.commands import CommandListener  # noqa: E402
from robot.dog.link.probe import extract_lowstate, extract_pose, safe_error  # noqa: E402
from robot.dog.planning.smart_patrol import (HeadingBandit, OccupancyGrid, PatrolPlanner, StallDetector, body_frame,  # noqa: E402
                              quaternion_yaw, sector_ranges, voxel_points_world, wrap_angle)
from robot.dog.planning.resident_policy import ResidentPolicy, RESOLVE_POSTURES
from robot.dog.perception.reid import spoken_name
from robot.dog.planning.missions import MissionBoard  # noqa: E402
from robot.dog.planning import agent as agent_mod  # noqa: E402
from robot.dog.perception.target_id import TargetIdentifier  # noqa: E402
try:
    from robot.dog.memory.graph import SpacetimeGraph  # noqa: E402
except Exception:
    SpacetimeGraph = None
try:
    from robot.dog.dimos.frontier import FrontierPlanner  # noqa: E402  (dimOS venv only)
except Exception:
    FrontierPlanner = None
try:
    from robot.dog.perception import objects as objects_mod  # noqa: E402
except Exception:  # optional: object detection needs cv2/ultralytics
    objects_mod = None
try:
    from robot.dog.memory.spacetime import SpacetimeRecorder  # noqa: E402
except Exception:  # the recorder module is optional until it lands
    SpacetimeRecorder = None
from robot.dog.link.walk import (MIN_OPERATING_SOC_PERCENT, MOTION_INHIBIT_PATH, TOPIC_LOWSTATE, TOPIC_POSE,  # noqa: E402
                      TOPIC_SPORT, _private_ipv4, _status_code)

SCRIPT_VERSION = "go2-patrol-greet/0.3"
BASE_HEIGHT_M = 0.32  # Go2 standing base height above the floor; anchors the LiDAR floor estimate
STAND_UP, BALANCE_STAND, STOP_MOVE, MOVE, HELLO, EULER = 1004, 1002, 1003, 1008, 1016, 1007
LOOK_UP_PITCH = -0.25  # body pitch (rad) that lifts the fixed head camera toward a close person's face; BalanceStand levels it
# Greeting tricks, one per new person in rotation: (name, sport api id, seconds to let it finish).
_TRICK_TABLE = {"hello": (HELLO, 4.0), "dance": (1022, 10.0), "heart": (1036, 6.0), "stretch": (1017, 5.0)}
# Default: a quick wave only, so greetings do not eat exploration time. ANNIE_GREET_TRICKS=hello,dance makes every
# other greeting a dance (showpiece mode, Henry 2026-09-20 demo).
GREET_TRICKS = [(n, *_TRICK_TABLE[n]) for n in (os.environ.get("ANNIE_GREET_TRICKS") or "hello").replace(" ", "").split(",")
                if n in _TRICK_TABLE] or [("hello", HELLO, 4.0)]
TOPIC_VOXELS, TOPIC_LIDAR_SWITCH, TOPIC_SPORT_STATE = "rt/utlidar/voxel_map_compressed", "rt/utlidar/switch", "rt/lf/sportmodestate"
TOPIC_AVOID, AVOID_SWITCH_SET, AVOID_USE_API = "rt/api/obstacles_avoid/request", 1001, 1004


class GreetPolicy:
    """Deterministic greeting decisions from tracker output. No hardware, no model."""

    def __init__(self, *, cooldown_s: float = 240.0, min_height_frac: float = 0.5, center_frac: float = 0.18,
                 follow_min_height_frac: float = 0.12, checkin_cooldown_s: float = 60.0):
        self.cooldown_s = cooldown_s
        self.min_height_frac = min_height_frac  # greet only when the person is this tall in frame (close)
        self.center_frac = center_frac  # ... and their box centre is within this fraction of the frame centre
        self.follow_min_height_frac = follow_min_height_frac  # smaller than this is too far to bother following
        self.checkin_cooldown_s = checkin_cooldown_s
        self.greeted: dict[int, float] = {}
        self.checked: dict[int, float] = {}
        self.residents = ResidentPolicy()
        self.ignored: dict[int, float] = {}  # track_id -> until; people we are done with for a while

    def ignore(self, track_id, *, now_s, for_s=120.0):
        self.ignored[track_id] = now_s + for_s

    def observe(self, tracks, place_by_tid, *, now_s):
        self.residents.observe(tracks, place_by_tid, now_s=now_s)
        for track in tracks:
            if track.get("posture") in RESOLVE_POSTURES:
                self.checked.pop(track.get("track_id"), None)

    def mark_checkin(self, track, place, *, now_s, radius_m=None):
        self.checked[track.get("track_id")] = now_s
        self.residents.mark_asked(track, place, now_s=now_s, radius_m=radius_m)

    def step(self, tracks, frame_w, frame_h, *, now_s, front_m=None, place_by_tid=None, radius_by_tid=None):
        """Return ('patrol', None) | ('follow', track_id) | ('greet', track_id) | ('checkin', track_id).

        `front_m` is the LiDAR range straight ahead when known: a centred person within 1.1 m
        is close enough to greet even when the camera crops them (box height under-reads).

        The nearest upright person (tallest box) is followed; they are greeted once they stand
        close and directly ahead, then followed again until the cooldown lets a new greeting through.
        """
        lying = [t for t in tracks if t.get("posture") == "lying" and t.get("lying_frames", 0) >= 6]
        for t in lying:
            tid = t.get("track_id")
            last = self.checked.get(tid)
            if (last is None or now_s - last >= self.checkin_cooldown_s) and self.residents.should_ask(
                    t, (place_by_tid or {}).get(tid), now_s=now_s, radius_m=(radius_by_tid or {}).get(tid),
                    visible_lying_tids={person.get("track_id") for person in lying}):
                return "checkin", tid
        people = []
        for t in tracks:
            tid = t.get("track_id")
            if tid is None or t.get("posture") == "lying":
                continue
            if self.ignored.get(tid, 0.0) > now_s:
                continue
            x1, y1, x2, y2 = t["box"]
            height = (y2 - y1) / float(frame_h)
            if height < self.follow_min_height_frac:
                continue
            offset = abs((x1 + x2) / 2.0 / float(frame_w) - 0.5)
            people.append((height, offset, tid))
        if not people:
            return "patrol", None
        height, offset, tid = max(people)  # nearest = tallest box
        last = self.greeted.get(tid)
        close = height >= self.min_height_frac or (front_m is not None and front_m <= 1.0 and height >= 0.3)
        if front_m is not None and front_m > 1.3:
            close = False  # the LiDAR says they are still far, whatever the box says
        if close and offset <= self.center_frac and (last is None or now_s - last >= self.cooldown_s):
            self.greeted[tid] = now_s
            return "greet", tid
        return "follow", tid

    @staticmethod
    def greeting_text(track) -> str:
        identity = track.get("identity") or {}
        name = spoken_name(identity)
        return f"Hi {name}, lovely to see you. How are you feeling today?" if name else "Hello there, lovely to see you. How are you doing?"


def _default_conn_factory(ip, aes_key):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key)


def _say(text):
    print(f"go2-patrol-greet: {text}", file=sys.stderr, flush=True)


def _speak_host(text: str):
    """Speak on the host speaker without blocking the control loop (ElevenLabs when configured, else say)."""
    threading.Thread(target=lambda: speak_blocking(text), daemon=True, name="speak").start()


def _say_blocking(text: str) -> bool:
    """macOS `say` (text on stdin, never as an argument), played through the selected speaker when one is
    chosen (rendered to a temp AIFF first), else straight to the system default; waits until done."""
    try:
        from robot.dog.voice import devices as devices_mod
        dev = devices_mod.shared()
        if dev.output_name:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as fh:
                path = fh.name
            try:
                rc = subprocess.run(["say", "-o", path], input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60).returncode
                if rc == 0:
                    return dev.play(Path(path).read_bytes(), suffix=".aiff")
            finally:
                Path(path).unlink(missing_ok=True)
        return subprocess.run(["say"], input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0
    except Exception:
        return False


def _whisper_transcribe(wav_bytes: bytes) -> str | None:
    from robot.simulation.local_stt import LocalSTTAdapter
    text = (LocalSTTAdapter().transcribe(wav_bytes).text or "").strip()
    return text or None


def _cloud_voice():
    """ElevenLabs speaks / Deepgram hears when their keys are in the environment; local fallback otherwise.
    Output goes through the selected speaker (AirPods, Mac speakers, the phone app...)."""
    from robot.dog.voice import devices as devices_mod
    from robot.dog.voice.cloud import CloudVoice
    return CloudVoice(local_speak=_say_blocking, local_transcribe=_whisper_transcribe, player=devices_mod.shared().play)


VOICE = None  # created lazily so tests never touch the environment


def voice():
    global VOICE
    if VOICE is None:
        VOICE = _cloud_voice()
    return VOICE


LISTENER = None  # the always-on wake-word listener of the running patrol, so speaking can mute it and open a conversation


def speak_blocking(text: str) -> bool:
    """Speak on the host speaker (ElevenLabs voice when configured, else macOS say); blocks until done. The
    always-on mic ignores Annie's own voice meanwhile and then listens without a wake word for a while."""
    lis = LISTENER
    if lis is not None:
        lis.mute(2.0 + 0.4 * len(text.split()))
    ok = voice().speak(text)
    if lis is not None:
        lis.mute(1.0)  # Discard the speaker's tail rather than hearing our own acknowledgment.
        lis.open_conversation(45.0)
    return ok


def listen_blocking(max_s: float) -> dict:
    """Host microphone -> utterance (Silero VAD) -> transcript (Deepgram when configured, else local Whisper)."""
    from robot.dog.voice import devices as devices_mod
    from robot.simulation.live_listener import capture_utterance, pcm16_to_wav, silero_vad
    lis = LISTENER
    if lis is not None:
        lis.mute(max_s + 1.5)  # this call owns the reply; the always-on listener must not answer it too
    recorder = devices_mod.shared().recorder()  # the selected microphone (AirPods, Mac, phone app...)
    try:
        utterance = capture_utterance(recorder, silero_vad(), max_ms=int(max_s * 1000))
    finally:
        with contextlib.suppress(Exception):
            recorder.close()
        if lis is not None:
            lis.muted_until = 0.0
    if utterance["outcome"] != "speech":
        return {"transcript": None, "heard": False, "speech_ms": 0}
    text = voice().transcribe(pcm16_to_wav(utterance["pcm"]))
    return {"transcript": text, "heard": bool(text), "speech_ms": utterance["speech_ms"], "stt": voice().stats}


def _encode(frame, width=480, quality=70):
    import cv2
    img = frame.to_ndarray(format="bgr24")
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes(), img.shape[1], img.shape[0]


def _fast_tracker(conf=0.30, imgsz=352, faces_dir=None):
    """Pose tracker at a reduced inference size for low-latency following.

    CPU on purpose: on Apple MPS the ultralytics NMS step hits its 2 s time limit on real
    frames (measured 3.5 s/frame live vs 36 ms on CPU at 416 px, 2026-09-19).
    """
    from robot.simulation.person_tracker import PersonTracker
    face_index = None
    with contextlib.suppress(Exception):  # enrolled, consenting people only; absent index -> no face matching
        from robot.simulation.face_id import DEFAULT_FACES_DIR, INDEX_FILENAME, FaceIndex
        index_path = Path(faces_dir or os.environ.get("ANNIE_FACES_DIR") or DEFAULT_FACES_DIR) / INDEX_FILENAME
        if index_path.exists():
            face_index = FaceIndex().load(index_path)
    return PersonTracker(conf=conf, device="cpu", imgsz=imgsz, face_index=face_index)


VIEW_HTML = r"""<!doctype html><meta charset="utf-8"><title>Annie live</title>
<style>body{margin:0;background:#111;color:#eee;font:14px/1.4 -apple-system,Helvetica,sans-serif}
.wrap{display:flex;gap:16px;padding:12px}img{width:720px;max-width:100%;border-radius:8px;background:#000}
.panel{min-width:260px}.k{color:#9aa}.big{font-size:22px;font-weight:600}.log{font-family:Menlo,monospace;font-size:12px;white-space:pre-wrap;color:#cfc}
</style><div class="wrap"><div><img id="f" src="/stream.mjpg"><div style="margin-top:8px"><img id="m" src="/map.jpg" style="width:360px;border-radius:8px;background:#000"><div class="k">LiDAR occupancy map (odom frame): light = obstacle, grey = visited, blue = trail, green = greeted, yellow = people seen, red = dog, magenta = explore heading</div></div></div><div class="panel">
<div class="big" id="mode">-</div><div><span class="k">battery</span> <span id="bat">-</span> &middot; <span class="k">t</span> <span id="t">-</span>s &middot; <span class="k">home</span> <span id="home">-</span> m</div>
<div><span class="k">lidar front/left/right</span> <span id="lidar">-</span></div>
<div><span class="k">perception</span> <span id="fps">-</span> fps</div>
<div><span class="k">people</span> <span id="people">-</span> &middot; <span class="k">greeted</span> <span id="greet">-</span> &middot; <span class="k">check-ins</span> <span id="chk">-</span></div>
<div style="margin:8px 0"><a href="/spacetime" target="_blank" style="color:#8cf">4D space-time view</a></div>
<div style="margin:8px 0"><button onclick="cmd('go_home')">Home base</button> <button onclick="cmd('stop')">Stop</button> <button onclick="cmd('explore')">Explore</button> <button onclick="cmd('scan')">Look around</button> <button onclick="cmd('dance')">Dance</button> <button onclick="cmd('hello')">Wave</button></div>
<div class="log" id="log"></div></div></div>
<script>
const g=id=>document.getElementById(id);
const cmd=a=>fetch('/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});
const f=document.getElementById('f');f.onerror=()=>setTimeout(()=>{f.src='/stream.mjpg?'+Date.now()},500);
setInterval(()=>{f.src='/stream.mjpg?'+Date.now()},90000);  // re-arm the multipart stream before Chrome gives up on it
const m=document.getElementById('m');setInterval(()=>{m.src='/map.jpg?'+Date.now()},400);
setInterval(async()=>{try{const s=await (await fetch('/status.json')).json();
g('mode').textContent=s.mode+(s.action&&s.action!=='patrol'?' / '+s.action:'');g('bat').textContent=s.battery==null?'-':s.battery.toFixed(0)+'%';
g('t').textContent=(s.t_s||0).toFixed(0);g('home').textContent=(s.home_m||0).toFixed(2);
const r=s.ranges||{};const fm=v=>v==null?'clear':v.toFixed(2)+' m';g('lidar').textContent=fm(r.front)+' / '+fm(r.left)+' / '+fm(r.right);
g('fps').textContent=(s.fps||0).toFixed(1);g('people').textContent=(s.tracks||[]).length;g('greet').textContent=s.greetings;g('chk').textContent=s.checkins;
g('log').textContent=(s.log||[]).join('\n');}catch(e){}},250);
</script>"""

COMMAND_CENTER_PATH = Path(__file__).resolve().parents[1] / "view" / "command_center.html"  # the operator page; VIEW_HTML is the fallback

SKELETON = [(5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16)]


class DemoEveryoneGrandma:
    """Explicit stage casting, never identity recognition or enrollment."""

    def apply(self, img, tracks):
        for track in tracks:
            track["identity"] = {"name": "Jeanine", "method": "demo_role", "demo": True}
        return tracks


def grandma_selection_status(view) -> dict:
    enabled = bool(getattr(view, "require_grandma_selection", False))
    empty = {"enabled": enabled, "name": None, "guest": None, "state": "unselected", "needs_selection": True}
    if not enabled:
        return empty
    try:
        return {**view.people.reid().selection_status(), "enabled": True}
    except Exception:
        return {**empty, "state": "unavailable"}


def telemetry_snapshot(view) -> dict:
    """Everything the command-center page shows in one bounded JSON: live state, the last brain decisions and
    voice commands, greetings/check-ins/collisions, mission receipts, the frontier goal and the graph's sentences.
    Reads the run's report dict from the server thread; every part is optional and failures degrade to empty."""
    with view.lock:
        state = json.loads(json.dumps(view.state))
        dialogue = list(getattr(view, "dialogue", []))
    report = getattr(view, "report", None) or {}
    out = {"state": state, "connected": (report.get("connection") or {}).get("status") == "connected",
           "source": report.get("source") or "hardware",
           "motion_enabled": report.get("motion_enabled", False), "paused": report.get("paused", False),
           "control_mode": "manual" if report.get("manual_control") else "autonomous", "dialogue": dialogue,
           "reason": report.get("reason"), "elapsed_s": report.get("elapsed_s"),
           "brain": {"enabled": bool((report.get("brain") or {}).get("enabled")), "period_s": getattr(view, "brain_period_s", None),
                     "decisions": list((report.get("brain") or {}).get("decisions") or [])[-8:]},
           "voice": {"commands": list((report.get("voice") or {}).get("commands") or [])[-6:],
                     **({k: v for k, v in VOICE.status().items() if k != "stats"} if VOICE is not None else {})},
           "greetings": [{"t_s": g.get("t_s"), "text": g.get("text"), "name": (g.get("identity") or {}).get("name") if isinstance(g.get("identity"), dict) else None}
                         for g in list(report.get("greetings") or [])[-6:]],
           "checkins": [{"t_s": c.get("t_s")} for c in list(report.get("checkins") or [])[-4:]],
           "collisions": [{"t_s": c.get("t_s"), "mode": c.get("mode"), "front_m": c.get("front_m")} for c in list(report.get("collisions") or [])[-4:]],
           "instructions": list(report.get("instructions") or [])[-6:],
           "conversations": list(report.get("conversations") or [])[-6:],
           "concerns": list(report.get("concerns") or [])[-4:],
           "remarks": list(report.get("remarks") or [])[-6:],
           "missions": [], "frontier": {"available": False, "goal": None}, "graph_sentences": [], "places": 0, "objects": [],
           "map": getattr(view, "map_geometry", None), "grandma_selection": grandma_selection_status(view),
           "demo_everyone_grandma": bool(getattr(view, "demo_everyone_grandma", False))}
    inf = getattr(view, "planning_inference", None)
    out["inference"] = {"enabled": bool(inf and inf.provider != "off"),
                        "provider": getattr(inf, "provider", "off"), "model": getattr(inf, "model", None),
                        "stats": dict(getattr(inf, "stats", {}))}
    board = getattr(view, "missions", None)
    if board is not None:
        with contextlib.suppress(Exception):
            out["missions"] = board.recent(12)
    frontier = getattr(view, "frontier", None)
    if frontier:
        goal = frontier.get("goal")
        out["frontier"] = {"available": frontier.get("planner") is not None,
                           "goal": [round(float(goal[0]), 2), round(float(goal[1]), 2)] if goal is not None else None}
    graph = getattr(view, "graph", None)
    if graph is not None:
        with contextlib.suppress(Exception):
            out["graph_sentences"] = list(graph.summary(limit=6).get("sentences") or [])[:6]
            out["places"] = len(graph.places())
    recorder = getattr(view, "recorder", None)
    if recorder is not None:
        with contextlib.suppress(Exception):
            latest = recorder.latest()
            out["objects"] = sorted({p["label"] for p in latest.get("people") or []
                                     if str(p.get("track_id", "")).startswith("obj-")})[:12]
    return out


class LiveView:
    """Annotated camera + status for the operator's browser; a stdlib HTTP server in a thread. Port 0 disables."""

    def __init__(self, port=8011, host="127.0.0.1", status_lines=12, token=None):
        self.port, self.host = port, host
        # Token: required for the state-changing routes (/command, /stop) when set; binding beyond loopback
        # without one is refused in main(). Host check: a browser page elsewhere cannot reach us via DNS rebinding.
        self.token = token if token is not None else (os.environ.get("ANNIE_BODY_TOKEN") or None)
        self.extra_hosts = {h.strip().lower() for h in os.environ.get("ANNIE_VIEW_HOSTS", "").split(",") if h.strip()}
        self.state = {"mode": "-", "action": None, "tracks": [], "ranges": None, "battery": None, "t_s": 0.0,
                      "greetings": 0, "checkins": 0, "home_m": 0.0, "log": []}
        self.status_lines = status_lines
        self.jpeg = None
        self.map_jpeg = None
        self.lock = threading.Lock()
        self.new_frame = threading.Condition(self.lock)
        self.frame_seq = 0
        self.dialogue = []
        self.dialogue_seq = 0
        self.server = None

    def record_dialogue(self, role, text, *, source, mocked=False):
        if not text:
            return
        with self.lock:
            self.dialogue_seq += 1
            self.dialogue.append({"id": self.dialogue_seq, "role": role, "text": str(text)[:500],
                                  "at_ms": int(time.time() * 1000), "source": source, "mocked": bool(mocked)})
            self.dialogue = self.dialogue[-80:]

    def start(self):
        if not self.port:
            return None
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _host_ok(self):
                host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
                ok = host in ("127.0.0.1", "localhost", "::1", view.host.lower()) or host in view.extra_hosts
                if not ok:
                    self._send(421, b'{"error":"unexpected Host"}', "application/json")
                return ok

            def _token_ok(self):
                if view.token and not hmac.compare_digest(self.headers.get("X-Body-Token") or "", view.token):
                    self._send(401, b'{"error":"unauthorized"}', "application/json")
                    return False
                return True

            def do_GET(self):
                if not self._host_ok():
                    return
                if self.path.startswith("/stream.mjpg"):
                    return self._stream()
                if self.path.startswith("/graph.json"):
                    g = getattr(view, "graph", None)
                    if g is None:
                        return self._send(503, b'{"error":"graph off"}', "application/json")
                    try:
                        q = parse_qs(urlsplit(self.path).query)
                        n_vox = max(0, min(int(q.get("voxels", ["0"])[0]), 20000))  # ?voxels=N embeds the remembered geometry
                        return self._send(200, json.dumps(g.snapshot(max_voxels=n_vox)).encode(), "application/json")
                    except ValueError:
                        return self._send(400, b'{"error":"voxels must be an integer"}', "application/json")
                    except Exception:
                        return self._send(500, b'{"error":"snapshot failed"}', "application/json")
                if self.path.startswith("/command/"):
                    board = getattr(view, "missions", None)
                    receipt = board.get(self.path[len("/command/"):].split("?")[0]) if board else None
                    return self._send(200, json.dumps(receipt).encode(), "application/json") if receipt \
                        else self._send(404, b'{"error":"unknown command_id"}', "application/json")
                if self.path.startswith("/people"):
                    people = getattr(view, "people", None)
                    if people is None:
                        return self._send(503, b'{"error":"people directory off"}', "application/json")
                    return self._send(200, json.dumps({"people": people.list()}).encode(), "application/json")
                if self.path.startswith("/voice"):
                    if getattr(view, "mock_audio", False):
                        return self._send(200, b'{"source":"simulation","mocked":true,"cloud":false,"speak_via":"mock","hear_via":"mock"}', "application/json")
                    v = VOICE
                    st = v.status() if v is not None else {"cloud": False, "elevenlabs": False, "deepgram": False, "speak_via": "off", "hear_via": "off"}
                    with contextlib.suppress(Exception):
                        from robot.dog.voice import devices as devices_mod
                        st["devices"] = devices_mod.shared().status()
                    return self._send(200, json.dumps(st).encode(), "application/json")
                if self.path.startswith("/health"):
                    return self._send(200, b'{"ok": true, "service": "go2-patrol-greet"}', "application/json")
                if self.path.startswith(("/spacetime", "/three.min.js")):
                    # the recorder module owns these routes (validated since/until/max_frames, page, offline Three.js)
                    from robot.dog.memory.spacetime import spacetime_http
                    resp = spacetime_http(getattr(view, "recorder", None), self.path)
                    if resp:
                        return self._send(*resp)
                if self.path.startswith("/map.jpg"):
                    data = getattr(view, "map_jpeg", None)
                    return self._send(200, data, "image/jpeg") if data else self._send(404, b"no map yet", "text/plain")
                if self.path.startswith("/frame.jpg"):
                    with view.lock:
                        data = view.jpeg
                    if data is None:
                        return self._send(404, b"no frame yet", "text/plain")
                    return self._send(200, data, "image/jpeg")
                if self.path.startswith("/status.json"):
                    with view.lock:
                        data = json.dumps(view.state).encode()
                    return self._send(200, data, "application/json")
                if self.path.startswith("/telemetry.json"):
                    try:
                        return self._send(200, json.dumps(telemetry_snapshot(view), allow_nan=False).encode(), "application/json")
                    except Exception:
                        return self._send(500, b'{"error":"telemetry failed"}', "application/json")
                if self.path.startswith("/classic"):
                    return self._send(200, VIEW_HTML.encode(), "text/html; charset=utf-8")
                try:
                    page = COMMAND_CENTER_PATH.read_bytes()  # read per request so edits show on reload
                except OSError:
                    page = VIEW_HTML.encode()
                self._send(200, page, "text/html; charset=utf-8")

            def do_POST(self):
                if not self._host_ok() or not self._token_ok():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if 0 < length < (8 << 20 if self.path.startswith("/people") else 65536) else b""
                board = getattr(view, "missions", None)
                if self.path == "/stop" and board is not None:  # body contract: cancel the mission, StopMove follows
                    if getattr(view, "commands", None) is not None:
                        view.commands["resume_requested"] = False
                    try:
                        payload = json.loads(raw.decode() or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError
                    except ValueError:
                        return self._send(400, b'{"error":"invalid JSON object"}', "application/json")
                    code, receipt = board.submit({"name": "stop", **({"command_id": payload["command_id"]} if "command_id" in payload else {})})
                    return self._send(code, json.dumps({**receipt, "cancelled": bool(receipt.get("stop_requested")),
                                                       "note": "Stop requested; poll /command/{command_id} for software acknowledgment."}).encode(), "application/json")
                if self.path == "/people/assign":
                    if not getattr(view, "require_grandma_selection", False):
                        return self._send(409, b'{"error":"Clothing selection is not enabled for this run."}', "application/json")
                    report = getattr(view, "report", {})
                    if (report.get("connection") or {}).get("status") != "connected":
                        return self._send(503, b'{"error":"Robot is not connected."}', "application/json")
                    if not report.get("paused") or (board is not None and board.executing() is not None):
                        return self._send(409, b'{"error":"Pause Annie before selecting Grandma."}', "application/json")
                    try:
                        payload = json.loads(raw.decode() or "{}")
                        if not isinstance(payload, dict) or set(payload) != {"guest"} or not isinstance(payload["guest"], str):
                            raise ValueError("Choose a visible Guest from the camera.")
                        result = view.people.reid().assign_guest(payload["guest"], name="Jeanine")
                    except (ValueError, TypeError) as exc:
                        return self._send(409, json.dumps({"error": str(exc)}).encode(), "application/json")
                    except AttributeError:
                        return self._send(503, b'{"error":"Clothing tracker is unavailable."}', "application/json")
                    view.log("Grandma selected for this demo using clothing; no face enrolled")
                    return self._send(200, json.dumps({**result, "enabled": True}).encode(), "application/json")
                if self.path.startswith("/people"):
                    people = getattr(view, "people", None)
                    if people is None:
                        return self._send(503, b'{"error":"people directory off"}', "application/json")
                    try:
                        payload = json.loads(raw.decode() or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError
                    except ValueError:
                        return self._send(400, b'{"error":"invalid JSON object"}', "application/json")
                    if self.path.startswith("/people/forget"):
                        ok = people.forget(str(payload.get("name") or "").strip())
                        return self._send(200 if ok else 404, json.dumps({"forgotten": ok}).encode(), "application/json")
                    import base64
                    photos = []
                    for b64 in (payload.get("photos") or [])[:10]:
                        with contextlib.suppress(Exception):
                            photos.append(base64.b64decode(b64, validate=False))
                    try:
                        result = people.enroll(str(payload.get("name") or ""), photos, relation=payload.get("relation"),
                                               shirt=payload.get("shirt"), notes=payload.get("notes"))
                    except ValueError as exc:
                        return self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    view.log(f"people: {result['name']} enrolled ({result['faces_added']} face(s))")
                    if result.get("shirt") and getattr(view, "identifier", None) is not None:
                        ident = view.identifier
                        ident.name, ident.colour = result["name"], result["shirt"]  # the shirt-colour identity follows the app
                    return self._send(200, json.dumps(result).encode(), "application/json")
                if self.path.startswith("/voice"):
                    if getattr(view, "mock_audio", False):
                        return self._send(409, b'{"error":"audio is mocked in simulation"}', "application/json")
                    try:
                        payload = json.loads(raw.decode() or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError
                    except ValueError:
                        return self._send(400, b'{"error":"invalid JSON object"}', "application/json")
                    v = voice()
                    keys = {k: payload.get(k) for k in ("eleven_key", "deepgram_key", "eleven_voice") if isinstance(payload.get(k), str) and len(payload[k]) <= 200}
                    cloud = payload.get("cloud") if isinstance(payload.get("cloud"), bool) else None
                    st = v.configure(cloud=cloud, **keys)
                    from robot.dog.voice import devices as devices_mod
                    dev = {k: payload.get(k) for k in ("input_device", "output_device") if isinstance(payload.get(k), str) and len(payload[k]) <= 80}
                    if dev:
                        devices_mod.shared().configure(input_name=dev.get("input_device"), output_name=dev.get("output_device"))
                        listener = getattr(view, "listener", None)
                        if listener is not None and "input_device" in dev:
                            listener.reopen(devices_mod.shared().recorder)  # the wake-word mic follows the selection
                    st["devices"] = {k: v_ for k, v_ in devices_mod.shared().status().items()}
                    view.log(f"voice settings: cloud={st['cloud']} speak={st['speak_via']} hear={st['hear_via']} "
                             f"mic={st['devices']['input']} speaker={st['devices']['output']}")  # never the keys
                    return self._send(200, json.dumps(st).encode(), "application/json")
                if self.path.startswith("/command"):
                    try:
                        payload = json.loads(raw.decode() or "{}")
                    except ValueError:
                        return self._send(400, b'{"error":"invalid JSON"}', "application/json")
                    if isinstance(payload, dict) and "name" in payload and board is not None:  # body-shaped mission
                        if payload.get("name") == "stop" and getattr(view, "commands", None) is not None:
                            view.commands["resume_requested"] = False
                        code, receipt = board.submit(payload)
                        view.log(f"mission {receipt.get('name', '?')} -> {code}")
                        return self._send(code, json.dumps(receipt).encode(), "application/json")
                    action = payload.get("action") if isinstance(payload, dict) else None
                    if action not in ("go_home", "stop", "explore", "scan", "dance", "hello", "sit", "stand", "follow"):
                        return self._send(400, b'{"error":"unknown action"}', "application/json")
                    cmds = getattr(view, "commands", None)
                    if cmds is None:
                        return self._send(503, b'{"error":"not running"}', "application/json")
                    if action != "stop" and not getattr(view, "motion_enabled", True):
                        return self._send(409, b'{"error":"no_motion: movement disabled"}', "application/json")
                    if action == "stop" and board is not None:
                        board.submit({"name": "stop"})
                    cmds["resume_requested"] = action != "stop"
                    cmds["intent"], cmds["until"] = action, time.monotonic() + (90.0 if action == "go_home" else 12.0)
                    cmds["source"] = "web"
                    view.log(f"web command: {action}")
                    return self._send(202, json.dumps({"accepted": action}).encode(), "application/json")
                self._send(404, b"not found", "text/plain")

            def _send(self, code, data, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self):
                """Push every annotated frame as it is produced (multipart MJPEG); one thread per viewer."""
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with view.new_frame:
                            if not view.new_frame.wait_for(lambda: view.frame_seq != seen, timeout=2.0):
                                continue
                            seen, data = view.frame_seq, view.jpeg
                        if data is None:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://{self.host}:{self.port}/"

    def log(self, line):
        with self.lock:
            self.state["log"] = (self.state["log"] + [line])[-self.status_lines:]

    def update(self, **fields):
        with self.lock:
            self.state.update({k: (None if v == float("inf") else v) for k, v in fields.items()})
            if "ranges" in fields and fields["ranges"]:
                self.state["ranges"] = {k: (None if v == float("inf") else round(v, 2))
                                        for k, v in fields["ranges"].items() if k in ("front", "left", "right")}

    def annotate(self, img, tracks, mode, ranges, battery, raw=None):
        """Draw boxes, keypoints, labels and a status banner; store the JPEG for /frame.jpg."""
        if not self.port:
            return
        try:
            import cv2
            out = img.copy()
            accepted = {t.get("track_id") for t in tracks}
            for t in raw or []:
                if t.get("track_id") not in accepted:  # detected but not (yet) trusted: thin grey box
                    x1, y1, x2, y2 = (int(v) for v in t["box"])
                    cv2.rectangle(out, (x1, y1), (x2, y2), (140, 140, 140), 1)
            for t in tracks:
                x1, y1, x2, y2 = (int(v) for v in t["box"])
                lying = t.get("posture") == "lying"
                color = (0, 0, 255) if lying else (0, 220, 0)
                cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
                name = ((t.get("identity") or {}).get("name")) or f"person {t.get('track_id')}"
                label = f"{name} {t.get('posture', '')} {t.get('conf', 0):.2f}"
                cv2.putText(out, label, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
                kps = t.get("keypoints") or []
                kpc = t.get("kp_conf") or []
                pts = [(int(p[0]), int(p[1])) if (i < len(kpc) and kpc[i] > 0.3) else None for i, p in enumerate(kps)]
                for a, b in SKELETON:
                    if a < len(pts) and b < len(pts) and pts[a] and pts[b]:
                        cv2.line(out, pts[a], pts[b], (255, 200, 0), 1, cv2.LINE_AA)
                for pnt in pts:
                    if pnt:
                        cv2.circle(out, pnt, 2, (255, 255, 0), -1)
            front = "clear" if not ranges or ranges["front"] == float("inf") else f"{ranges['front']:.2f} m"
            banner = f"{mode}  front {front}  battery {battery:.0f}%" if battery is not None else mode
            cv2.rectangle(out, (0, 0), (out.shape[1], 22), (0, 0, 0), -1)
            cv2.putText(out, banner, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
            ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with self.new_frame:
                    self.jpeg, self.frame_seq = buf.tobytes(), self.frame_seq + 1
                    self.new_frame.notify_all()
        except Exception:
            pass

    def stop(self):
        if self.server:
            self.server.shutdown()


def _person_places(tracks, frame_w, frame_h, pose, yaw, front_m=None):
    """Current-frame person position estimates for resident episodes, independent of recording."""
    places, radii = {}, {}
    if pose is None:
        return places, radii
    for track in tracks:
        tid = track.get("track_id")
        if tid is None:
            continue
        x1, y1, x2, y2 = track["box"]
        cx = (x1 + x2) / (2.0 * frame_w)
        height = max(0.05, (y2 - y1) / frame_h)
        distance = front_m if front_m is not None and abs(cx - 0.5) < 0.18 else min(6.0, 0.55 / height)
        bearing = yaw + (0.5 - cx) * math.radians(100.0)
        places[tid] = (pose[0] + distance * math.cos(bearing), pose[1] + distance * math.sin(bearing))
        radii[tid] = min(0.9, max(0.3, distance * 0.25))
    return places, radii


def _mission_person_pool(tracks, wanted, identity_cache, now, *, identity_ttl_s=2.0):
    """Named missions never fall back to strangers; tolerate short same-track ID flicker."""
    pool = []
    for track in tracks:
        tid = track.get("track_id")
        if tid is None:
            continue
        identity = track.get("identity") or {}
        if identity.get("name"):
            identity_cache[tid] = (now, dict(identity))
        elif tid in identity_cache:
            seen_at, cached = identity_cache[tid]
            if now - seen_at <= identity_ttl_s:
                identity = cached
        if not wanted or str(identity.get("name") or "").lower() == wanted:
            pool.append(dict(track, identity=dict(identity) if identity else None))
    for tid, (seen_at, _) in list(identity_cache.items()):
        if now - seen_at > identity_ttl_s:
            identity_cache.pop(tid, None)
    return pool


class MissionInterrupted(asyncio.CancelledError):
    """Safety interruption, including through best-effort firmware request handlers."""


async def run_patrol_greet(*, ip, aes_key, duration_s=300.0, speed_mps=0.25, yaw_rps=0.5, boundary_m=2.5,
                           rate_hz=15.0, stale_s=1.0, min_soc=MIN_OPERATING_SOC_PERCENT, conn_factory=None,
                           tracker=None, encoder=_encode, speak=_speak_host, policy=None, status=_say,
                           planner=None, stall=None, lidar=True, firmware_avoid=False, lidar_stale_s=2.0,
                           stop_on_checkin=False, idle_trick_s=45.0, view=None, no_motion=False, diag_every_s=5.0,
                           imgsz=352, voxel_min_interval_s=0.25, brain=None, brain_period_s=4.0, memory=None,
                           voice=None, bandit=None, identifier=None, recorder=None, frontier_planner="auto",
                           source="hardware", audio=None, faces_dir=None, start_paused=False, manual_control=False,
                           require_grandma_selection=False, demo_everyone_grandma=False, autonomous_demo=False):
    if require_grandma_selection and demo_everyone_grandma:
        raise ValueError("Choose clothing selection or everyone-as-Grandma demo mode, not both")
    if autonomous_demo and (not demo_everyone_grandma or manual_control or start_paused):
        raise ValueError("Autonomous demo requires everyone-as-Grandma mode without manual or paused startup")
    if demo_everyone_grandma and not autonomous_demo:
        manual_control = True
    start_paused = bool(start_paused or manual_control)
    if audio is not None and getattr(audio, "source", None) == "simulation" and source != "simulation":
        raise ValueError("mock audio requires simulation")
    if audio is not None:
        speak = audio.speak
    blocking_speak = audio.speak if audio is not None else speak_blocking
    blocking_listen = audio.listen if audio is not None else listen_blocking
    faces_dir = faces_dir or os.environ.get("ANNIE_FACES_DIR") or (".data/sim/faces" if source == "simulation" else ".data/faces")
    audio_where = "simulation mock" if audio is not None and getattr(audio, "source", None) == "simulation" else None
    loop = asyncio.get_running_loop()
    view = view or LiveView(port=0)
    view.require_grandma_selection = bool(require_grandma_selection)
    view.demo_everyone_grandma = bool(demo_everyone_grandma)
    original_speak, original_blocking_speak, original_listen = speak, blocking_speak, blocking_listen

    def spoken(text):
        result = original_speak(text)
        if result is not False:
            view.record_dialogue("annie", text, source=source, mocked=bool(audio_where))
        return result

    def spoken_blocking(text):
        result = original_blocking_speak(text)
        if result:
            view.record_dialogue("annie", text, source=source, mocked=bool(audio_where))
        return result

    def heard_blocking(max_s):
        result = original_listen(max_s)
        if isinstance(result, dict) and result.get("transcript"):
            view.record_dialogue("resident", result["transcript"], source=source, mocked=bool(audio_where))
        return result

    speak, blocking_speak, blocking_listen = spoken, spoken_blocking, heard_blocking
    view.mock_audio = bool(audio_where)
    view.motion_enabled = not no_motion
    view_url = view.start()
    if view_url:
        status(f"live view at {view_url}")
    _status = status

    def status(text):
        view.log(text)
        _status(text)

    report = {"script": SCRIPT_VERSION, "source": source, "motion_enabled": not no_motion, "paused": bool(start_paused), "manual_control": bool(manual_control), "target_ip": ip, "connection": {"status": "not_started"},
              "battery_soc_start": None, "frames": 0, "moves_sent": 0, "greetings": [], "checkins": [],
              "collisions": [], "modes": {}, "lidar": {"maps": 0, "first_ranges": None, "stale_ticks": 0},
              "firmware_avoid": None, "elapsed_s": 0.0, "max_distance_from_origin_m": 0.0, "reason": None,
              "completed": False, "stop": {"requested": False, "ack_ms": None, "code": None},
              "notes": ["Software StopMove only; host cannot stop the robot over a lost link.",
                        "Audio is mocked; no playback or recording." if audio_where else "Speech is played on the host, not the robot.",
                        "Collision detection = LiDAR voxel sectors + odometry stall; no touch sensors on this robot."]}
    policy = policy or GreetPolicy()
    planner = planner or PatrolPlanner(cruise_mps=speed_mps, turn_rps=yaw_rps, leash_m=boundary_m * 0.8,
                                       stop_m=0.6, clear_m=0.95, slow_m=1.4, min_turn_s=0.4)  # earlier stop: the map is 1-2 Hz
    stall = stall or StallDetector()
    guard_state = {"cam_log": 0.0}
    if tracker is None:
        tracker = _fast_tracker(imgsz=imgsz, faces_dir=faces_dir)
    diag = Diag()
    memory = memory or SightingMemory()
    if recorder is not None:
        _memory_add = memory.add

        def _add_and_record(*, t, pose, yaw, kind, people=0, note=None):
            entry = _memory_add(t=t, pose=pose, yaw=yaw, kind=kind, people=people, note=note)
            with contextlib.suppress(Exception):
                recorder.record_event(time.time(), pose[0], pose[1], kind, note or kind)
            if graph is not None:
                with contextlib.suppress(Exception):
                    graph.ingest_event(time.time(), pose[0], pose[1], kind, note or kind)
            return entry
        memory.add = _add_and_record
    brain_state = {"decision": None, "until": 0.0, "busy": False, "last": None, "count": 0}
    voice_state = {"intent": None, "until": 0.0, "count": 0}
    report["brain"] = {"enabled": brain is not None, "decisions": [], "failures": 0}
    report["voice"] = {"enabled": voice is not None, "commands": []}
    tel = {"soc": None, "low_t": None, "pose": None, "pose_t": None, "yaw": 0.0, "ranges": None, "ranges_t": None,
           "range_obstacle": None}
    latest = {"frame": None, "seq": 0}
    stopped = False
    conn = None
    seq = 0

    def on_low(msg):
        try:
            tel["soc"], tel["low_t"] = extract_lowstate(msg)["battery"]["soc_percent"], loop.time()
        except Exception:
            pass

    def on_pose(msg):
        try:
            pose = extract_pose(msg)
            p = pose["position"]
            tel["pose"], tel["pose_t"], tel["z"] = (p["x"], p["y"]), loop.time(), p["z"]
            tel["yaw"] = quaternion_yaw(pose["orientation_xyzw"])
        except Exception:
            pass

    voxel_q: queue.Queue = queue.Queue(maxsize=1)
    grid = {"map": None, "rendered": 0.0}

    def voxel_worker():
        """Reduce voxel maps to sector ranges off the event loop; newest map wins, at most ~4 Hz."""
        last = 0.0
        while not stopped:
            try:
                decoded, data, pose, yaw, z = voxel_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if time.monotonic() - last < voxel_min_interval_s:
                continue
            t0 = time.perf_counter()
            try:
                world = voxel_points_world(decoded, data)
                pts = body_frame(world, pose, yaw)
                floor = None if z is None else z - BASE_HEIGHT_M
                ranges = sector_ranges(pts, ground_hint=floor)
                obs = ranges.pop("obstacles_body", None)
                if grid["map"] is not None and obs is not None and len(obs):
                    # back to world: rotate body-frame obstacle points by yaw and translate by pose
                    import numpy as np
                    c, sn = math.cos(yaw), math.sin(yaw)
                    wx = pose[0] + c * obs[:, 0] - sn * obs[:, 1]
                    wy = pose[1] + sn * obs[:, 0] + c * obs[:, 1]
                    world_obs = np.stack([wx, wy, obs[:, 2]], axis=1)
                    grid["map"].observe(world_obs)
                    grid["map"].visit(pose[0], pose[1])
                    if recorder is not None:
                        with contextlib.suppress(Exception):
                            recorder.record_obstacles(time.time(), world_obs)
                    if graph is not None:
                        with contextlib.suppress(Exception):
                            graph.ingest_obstacles(time.time(), world_obs)
                    if time.monotonic() - grid["rendered"] > 0.4 and view.port:
                        grid["rendered"] = time.monotonic()
                        view.map_jpeg = grid["map"].render(pose, yaw, planner.target_heading, palette="costmap")
                        view.map_geometry = grid["map"].geometry()
            except Exception:
                continue
            tel["ranges"], tel["ranges_t"] = ranges, loop.time()
            if report["lidar"]["first_ranges"] is None:
                report["lidar"]["first_ranges"] = {k: (None if v == float("inf") else round(v, 2))
                                                   for k, v in ranges.items() if k != "points"}
            diag.sample("voxel_ms", (time.perf_counter() - t0) * 1000)
            last = time.monotonic()

    threading.Thread(target=voxel_worker, daemon=True, name="voxel").start()

    def on_voxels(msg):
        # The driver already decoded the voxel payload; hand it to the worker thread (never block the loop).
        try:
            data = msg.get("data") or {}
            decoded = data.get("data")
            if not isinstance(decoded, dict) or tel["pose"] is None:
                return
            try:
                voxel_q.put_nowait((decoded, data, tel["pose"], tel["yaw"], tel.get("z")))
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    voxel_q.get_nowait()
                voxel_q.put_nowait((decoded, data, tel["pose"], tel["yaw"], tel.get("z")))
            report["lidar"]["maps"] += 1
            if report["lidar"]["first_ranges"] is None:
                report["lidar"]["first_ranges"] = {k: (None if v == float("inf") else round(v, 2))
                                                   for k, v in tel["ranges"].items() if k != "points"}
        except Exception:
            pass

    def on_sport_state(msg):
        try:
            ro = (msg.get("data") or {}).get("range_obstacle")
            if isinstance(ro, list) and len(ro) == 4:
                tel["range_obstacle"] = [round(float(v), 2) for v in ro]
        except Exception:
            pass

    def convert(frame):
        """Video frame -> small BGR array (no JPEG round trip)."""
        import cv2
        img = frame.to_ndarray(format="bgr24")
        h, w = img.shape[:2]
        if w > 480:
            img = cv2.resize(img, (480, int(h * 480 / w)), interpolation=cv2.INTER_AREA)
        return img

    def annotate(img, tracks, raw, ctx):
        if objects_mod is not None and ctx.get("objects"):
            with contextlib.suppress(Exception):
                img = objects_mod.annotate(img, ctx["objects"])
        view.annotate(img, tracks, ctx.get("mode", "-"), ctx.get("ranges"), ctx.get("battery"), raw=raw)

    if conn_factory is not None and encoder is not _encode:  # tests inject an encoder returning (jpeg, w, h)
        def convert(frame):  # noqa: F811
            import numpy as np
            jpeg, w, h = encoder(frame)
            return np.zeros((h, w, 3), dtype=np.uint8)
    detector = None
    if objects_mod is not None and conn_factory is None:
        # Open vocabulary (YOLO-World: door, table, chair, bag, phone, ...) when its baked clip-free checkpoint exists,
        # else the COCO subset. Built here, warmed off the control thread below (first detect costs 5-9 s).
        with contextlib.suppress(Exception):
            if objects_mod.find_checkpoint(objects_mod.baked_name(objects_mod.HOME_VOCABULARY)) is not None \
                    and os.environ.get("ANNIE_OBJECTS", "open_vocab") == "open_vocab":
                detector = objects_mod.ObjectDetector.open_vocab(imgsz=416)
            elif objects_mod.find_checkpoint() is not None:
                detector = objects_mod.ObjectDetector()
    perception = Perception(tracker, convert=convert, annotate=annotate if view.port else None, diag=diag,
                            min_conf=0.45, min_keypoints=4, min_age_ms=250, identifier=identifier, objects=None, objects_every=8)
    if detector is not None:
        def _warm_detector():  # the pipeline only gets the detector once it is warm: no concurrent first inference
            try:
                import numpy as _np
                detector.detect(_np.zeros((270, 480, 3), dtype=_np.uint8), 0)
                perception.objects = detector
                status(f"objects: {'open-vocabulary' if getattr(detector, 'vocabulary', None) else 'COCO subset'} detector warm")
            except Exception as exc:
                status(f"objects: detector warm-up failed ({type(exc).__name__}); running without it")
        threading.Thread(target=_warm_detector, daemon=True, name="objects-warm").start()
    view.commands = voice_state  # POST /command on the live view sets the same bounded override as a voice command
    view.identifier = identifier
    with contextlib.suppress(Exception):
        from robot.dog.perception.people import PeopleDirectory
        view.people = PeopleDirectory(faces_dir=faces_dir, tracker=tracker)  # shared tracker receives enrollment updates
        view.people._index = getattr(tracker, "face_index", None)  # reuse the already loaded matching index
        if identifier is None or isinstance(identifier, TargetIdentifier):
            perception.identifier = view.people.reid(
                shirt_rules=lambda: [(identifier.name, identifier.colour)] if isinstance(identifier, TargetIdentifier) else [],
                pose_source=lambda: tel["pose"])
    if demo_everyone_grandma:
        perception.identifier = DemoEveryoneGrandma()
    view.recorder = recorder
    missions = MissionBoard()
    view.missions = missions
    report["missions"] = []
    graph = SpacetimeGraph() if SpacetimeGraph is not None else None  # the 4D knowledge graph (geometry + semantics)
    if graph is not None and recorder is not None and getattr(recorder, "path", None) and conn_factory is None:
        # persistence across restarts: what the previous run(s) saw in the last hour comes back before the first frame
        try:
            counts = graph.reload_recent(recorder.path, max_age_s=float(os.environ.get("ANNIE_MEMORY_RELOAD_S", "3600")))
            if counts["used"]:
                status(f"memory: reloaded {counts['used']} records from {recorder.path} (last hour of the recording)")
                report["memory_reloaded"] = counts
        except Exception as exc:
            status(f"memory reload skipped: {type(exc).__name__}")
    view.graph = graph
    planner_obj = (FrontierPlanner() if FrontierPlanner is not None else None) if frontier_planner == "auto" else frontier_planner
    frontier = {"planner": planner_obj, "goal": None, "t": 0.0}
    report["frontier"] = {"available": frontier["planner"] is not None, "goals": 0}
    view.report, view.frontier, view.brain_period_s = report, frontier, brain_period_s  # /telemetry.json reads these
    narrator = agent_mod.Narrator()
    instruct_state = {"thread": None, "receipt": None, "plan": None}
    situation_cache = {"t": 0.0, "sit": None}

    def build_situation(now_wall, *, tracks=(), ranges=None, home_m=0.0, battery=None, mode="-", max_age_s=1.0):
        """The agent's picture of the moment (graph entities with bearings, under-cover state, recent greetings)."""
        if situation_cache["sit"] is not None and now_wall - situation_cache["t"] < max_age_s:
            return situation_cache["sit"]
        pose = tel["pose"] or (0.0, 0.0)
        entities, sentences, over = [], [], None
        if graph is not None:
            with contextlib.suppress(Exception):
                entities = graph.snapshot(max_entities_out=40, max_events_out=0, max_track_out=2)["entities"]
                sentences = graph.summary(limit=6)["sentences"]
                over = graph.overhead(pose[0], pose[1])
        if over and over.get("covered"):
            uc = report.setdefault("under_cover", {})
            if not uc.get("since"):
                uc.update(since=now_wall, entry_heading_deg=round(math.degrees(tel["yaw"])), entry_xy=[round(pose[0], 2), round(pose[1], 2)])
            over.update(since_s=round(now_wall - uc["since"], 1), entry_heading_deg=uc["entry_heading_deg"],
                        exit_heading_deg=(uc["entry_heading_deg"] + 180) % 360 - 180)
        elif report.get("under_cover"):
            report["under_cover"] = {}
        sit = agent_mod.situation(now=now_wall, pose_xy=pose, yaw=tel["yaw"], entities=entities, graph_sentences=sentences, overhead=over,
                                  greeted=[g for g in report["greetings"] if "t" in g], tracks=tracks, ranges=ranges, home_m=home_m,
                                  battery=battery, mode=mode)
        sit["demo_everyone_grandma"] = bool(demo_everyone_grandma)
        situation_cache.update(t=now_wall, sit=sit)
        return sit
    view.situation = build_situation

    def line_inference():
        """A short-timeout client for spoken lines (greetings, remarks); None when the provider is not configured,
        and None under the fake dog (tests never call a model)."""
        if conn_factory is not None or os.environ.get("ANNIE_LLM_PROVIDER", "").lower() in ("off", "none"):
            return None
        try:
            from robot.dog.inference import Inference
            return Inference(timeout_s=4.0)
        except Exception:
            return None
    chat_client = line_inference()
    if conn_factory is None:
        with contextlib.suppress(Exception):
            from robot.dog.inference import shared as _shared
            view.planning_inference = _shared()

    def plan_instruct(receipt, sit, plan_slot):
        """Runs in a thread: instruction -> steps through the shared inference client (or the rules), then chain."""
        try:
            inf = None
            with contextlib.suppress(Exception):
                from robot.dog.inference import shared as _shared
                inf = _shared()
                view.planning_inference = inf
            from robot.dog.runtime.body import validate_command as _validate
            plan = agent_mod.plan_instruction(receipt["args"]["text"], sit, inference=inf, validate_command=_validate)
        except Exception as exc:
            plan = {"reply": f"planning failed ({type(exc).__name__})", "steps": [], "source": "error", "rejected": []}
        # A cancelled planner may finish after a new instruction has taken over.
        # Its result belongs only to the receipt that requested it.
        if plan_slot.get("receipt") is receipt and receipt.get("state") == "executing":
            plan_slot["plan"] = plan

    async def on_track(track):
        while not stopped:
            try:
                frame = await track.recv()
            except Exception:
                return
            latest["frame"], latest["seq"] = frame, latest["seq"] + 1
            perception.submit(frame)

    async def heartbeat():
        """Event-loop lag: how late a 50 ms sleep wakes up. Big numbers mean something is hogging the loop."""
        last_publish = loop.time()
        while not stopped:
            t0 = loop.time()
            await asyncio.sleep(0.05)
            diag.sample("loop_lag_ms", max(0.0, (loop.time() - t0 - 0.05) * 1000))
            if loop.time() - last_publish >= 0.25:
                # Keep diagnostics live even while the behaviour loop awaits speech.
                # Publishing never increments the safety or control counters.
                view.update(diag=diag.snapshot())
                last_publish = loop.time()

    async def request(api_id, priority=False, timeout=5.0):
        options = {"api_id": api_id}
        if priority:
            options["priority"] = 1
        resp = await asyncio.wait_for(conn.datachannel.pub_sub.publish_request_new(TOPIC_SPORT, options), timeout)
        return _status_code(resp)

    async def request_params(api_id, params: dict, timeout=5.0):
        options = {"api_id": api_id, "parameter": json.dumps(params)}
        resp = await asyncio.wait_for(conn.datachannel.pub_sub.publish_request_new(TOPIC_SPORT, options), timeout)
        return _status_code(resp)

    def guarded_velocity(vx, wz):
        """Final forward-motion boundary; autonomous modes cannot bypass sensor guards."""
        if vx > 0:
            if lidar and (tel["ranges_t"] is None or loop.time() - tel["ranges_t"] > lidar_stale_s):
                return 0.0, 0.0
            if (tel["ranges"] or {}).get("front", float("inf")) < max(0.4, planner.stop_m):
                vx = 0.0
            current = perception.latest()
            height = current.get("h") or 480
            for obj in current.get("objects") or []:
                x1, y1, x2, y2 = obj["box"]
                if y2 - y1 > 0.55 * height and y2 > 0.85 * height and obj.get("hits", 1) >= 2:
                    vx = 0.0
                    break
        return vx, wz

    def send_move(vx, wz):
        nonlocal seq, cmd_vx
        if no_motion:
            return
        vx, wz = guarded_velocity(vx, wz)
        cmd_vx = vx  # stall detection must use sent motion, not a proposal the guard rejected
        seq += 1
        conn.datachannel.pub_sub.publish_without_callback(
            TOPIC_SPORT, data={"header": {"identity": {"id": seq, "api_id": MOVE}},
                               "parameter": json.dumps({"x": float(vx), "y": 0.0, "z": float(wz)})}, msg_type="req")
        report["moves_sent"] += 1
        diag.tick("motion_tx")

    async def process_stop_requests(*, force=False):
        pending = missions.take_stops()
        if not pending and not force:
            return None
        send_move(0.0, 0.0)
        began, code, error = loop.time(), None, None
        try:
            code = await request(STOP_MOVE, priority=True, timeout=2.0)
        except asyncio.CancelledError:
            error = "StopMove interrupted before acknowledgment"
            raise
        except Exception as exc:
            error = f"StopMove failed: {type(exc).__name__}"
        finally:
            ack_ms = round((loop.time() - began) * 1000, 1)
            missions.finish_stops(pending, code=code, ack_ms=ack_ms, error=error)
        return {"code": code, "ack_ms": ack_ms, "error": error}

    commanded = False
    try:
        for attempt in range(3):  # a stale WebRTC slot on the robot clears in ~15 s
            conn = conn_factory(ip, aes_key) if conn_factory else _default_conn_factory(ip, aes_key)
            task = asyncio.create_task(conn.connect())
            while getattr(conn, "video", None) is None and not task.done():
                await asyncio.sleep(0.005)
            if getattr(conn, "video", None) is not None:
                conn.video.add_track_callback(on_track)
            try:
                await asyncio.wait_for(task, 25)
                break
            except Exception as exc:
                status(f"connect attempt {attempt + 1} failed ({type(exc).__name__}); retrying")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.disconnect(), 5)
                conn = None
                await asyncio.sleep(12)
        if conn is None:
            report["connection"]["status"] = "connect_failed"
            report["reason"] = "connect_failed"
            return report
        report["connection"]["status"] = "connected"
        await asyncio.wait_for(conn.datachannel.disableTrafficSaving(True), 10)
        conn.datachannel.pub_sub.subscribe(TOPIC_LOWSTATE, on_low)
        conn.datachannel.pub_sub.subscribe(TOPIC_POSE, on_pose)
        conn.datachannel.pub_sub.subscribe(TOPIC_SPORT_STATE, on_sport_state)
        if lidar:
            conn.datachannel.pub_sub.subscribe(TOPIC_VOXELS, on_voxels)
            with contextlib.suppress(Exception):
                conn.datachannel.pub_sub.publish_without_callback(TOPIC_LIDAR_SWITCH, "on")
        if firmware_avoid:
            # Firmware LiDAR avoidance service; optional because the Air firmware may refuse it.
            try:
                resp = await asyncio.wait_for(conn.datachannel.pub_sub.publish_request_new(
                    TOPIC_AVOID, {"api_id": AVOID_SWITCH_SET, "parameter": json.dumps({"enable": True})}), 5)
                report["firmware_avoid"] = {"switch_code": _status_code(resp)}
            except Exception as exc:
                report["firmware_avoid"] = {"switch_code": None, "error": type(exc).__name__}
        conn.datachannel.switchVideoChannel(True)
        deadline = loop.time() + 10
        while loop.time() < deadline and (latest["frame"] is None or tel["low_t"] is None or tel["pose_t"] is None):
            await asyncio.sleep(0.02)
        if latest["frame"] is None or tel["low_t"] is None or tel["pose_t"] is None:
            report["reason"] = "camera_or_telemetry_missing"
            return report
        report["battery_soc_start"] = tel["soc"]
        if tel["soc"] < min_soc:
            report["reason"] = "battery_low"
            return report
        perception.start()
        heartbeat_task = asyncio.create_task(heartbeat())
        origin = tel["pose"]
        grid["map"] = OccupancyGrid(origin)
        lidar_state = "waiting" if lidar else "off"
        if lidar:
            deadline = loop.time() + 4
            while loop.time() < deadline and tel["ranges"] is None:
                await asyncio.sleep(0.05)
            lidar_state = f"live {report['lidar']['first_ranges']}" if tel["ranges"] is not None else "no voxel maps yet"
        status(f"connected, battery {tel['soc']:.0f}%, camera live, lidar {lidar_state}, "
               f"firmware_avoid={report['firmware_avoid']}, range_obstacle={tel['range_obstacle']}, "
               f"origin {origin[0]:.2f},{origin[1]:.2f}; {'held/paused' if start_paused else 'perception only' if no_motion else 'patrolling'}")
        commanded = not no_motion
        for api in () if no_motion or start_paused else (STAND_UP, BALANCE_STAND):
            for attempt in range(3):  # the firmware answers -1 / nothing while still finishing a previous motion
                try:
                    code = await request(api, timeout=4.0)
                except asyncio.TimeoutError:
                    code = "no_ack"
                if code in (0, None):
                    break
                await asyncio.sleep(1.5)
            else:
                report["reason"] = f"stand_failed:{code}"
                return report
            await asyncio.sleep(1.5)

        start = loop.time()
        last_seq = 0
        last_print = start
        tick = 1.0 / rate_hz
        tracks = []
        fw = fh = None
        cmd_vx = 0.0
        mode = "cruise"
        last_show = start
        last_diag = start
        last_seen = {"tid": None, "t": None, "side": 1.0}  # where the followed person was last, for the search turn
        memory_last_seen = [-1e9]
        follow_hold = {"tid": None, "since": None}
        follow_hold_s = 8.0
        blocked_times: list[float] = []
        rec_last = [-1e9]
        mission_state: dict = {}
        navigation_state: dict = {}
        mission_paused = bool(start_paused)
        report["paused"] = mission_paused
        needs_stand = bool(start_paused and not no_motion)
        mission_hold_until = [0.0]
        last_people_world = []
        last_remark_check = 0.0
        rec_objects_seq = [-1]
        bandit = bandit or HeadingBandit()
        bandit_state = {"until": 0.0, "rewarded": False}
        report["bandit"] = None

        def mission_guard(receipt, *, moving=False, forward=False):
            """Recheck live safety inputs at every nested-loop tick and after inference."""
            now_guard = loop.time()
            report["elapsed_s"] = round(now_guard - start, 1)
            if missions.stop_requested or receipt["state"] != "executing":
                raise MissionInterrupted("stopped by operator")
            if now_guard - start >= duration_s:
                raise MissionInterrupted("duration_complete")
            if tel["soc"] is None or tel["soc"] < min_soc:
                raise MissionInterrupted("battery_low")
            if any(tel[k] is None or now_guard - tel[k] > stale_s for k in ("low_t", "pose_t")):
                raise MissionInterrupted("telemetry_stale")
            distance = math.dist(origin, tel["pose"])
            report["max_distance_from_origin_m"] = max(report["max_distance_from_origin_m"], round(distance, 3))
            if distance > boundary_m:
                raise MissionInterrupted("boundary_exceeded")
            if moving and no_motion:
                raise MissionInterrupted("no_motion: movement disabled")
            if forward:
                if lidar and (tel["ranges_t"] is None or now_guard - tel["ranges_t"] > lidar_stale_s):
                    raise MissionInterrupted("lidar_stale")
                if (tel["ranges"] or {}).get("front", float("inf")) < 0.6:
                    raise MissionInterrupted("obstacle ahead")
                current = perception.latest()
                for obj in current.get("objects") or []:
                    x1, y1, x2, y2 = obj["box"]
                    height = current.get("h") or 480
                    if y2 - y1 > 0.55 * height and y2 > 0.85 * height and obj.get("hits", 1) >= 2:
                        raise MissionInterrupted("camera obstacle ahead")
            diag.tick("safety")

        async def mission_wait(receipt, seconds):
            deadline = loop.time() + seconds
            while loop.time() < deadline:
                mission_guard(receipt)
                await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
            mission_guard(receipt)

        async def mission_turn(receipt, degrees, timeout):
            mission_guard(receipt, moving=True)
            previous_yaw, turned = tel["yaw"], 0.0
            target = math.radians(degrees)
            deadline = loop.time() + timeout
            try:
                while True:
                    mission_guard(receipt, moving=True)
                    turned += wrap_angle(tel["yaw"] - previous_yaw)
                    previous_yaw = tel["yaw"]
                    error = target - turned
                    if abs(error) < 0.12:
                        return math.degrees(error)
                    if loop.time() >= deadline:
                        raise MissionInterrupted("turn timeout: target not reached")
                    send_move(0.0, math.copysign(max(0.8, min(1.0, 1.5 * abs(error))), error))
                    await mission_wait(receipt, 0.05)
            finally:
                send_move(0.0, 0.0)

        async def mission_walk(receipt, metres, timeout):
            mission_guard(receipt, moving=True)
            p0, want, forward = tel["pose"], abs(metres), metres > 0
            deadline = loop.time() + timeout
            stall.update(now_s=loop.time(), pose_xy=tel["pose"], commanded_vx=0.0)
            try:
                while True:
                    mission_guard(receipt, moving=True)
                    gone = math.dist(p0, tel["pose"])
                    if gone >= want:
                        return gone
                    if loop.time() >= deadline:
                        raise MissionInterrupted("walk timeout: target not reached")
                    mission_guard(receipt, moving=True, forward=forward)
                    if stall.update(now_s=loop.time(), pose_xy=tel["pose"], commanded_vx=0.3 if forward else 0.2):
                        raise MissionInterrupted("odometry stalled")
                    send_move(min(0.35, max(0.2, 0.4 * (want - gone))) if forward else -0.2, 0.0)
                    await mission_wait(receipt, 0.05)
            finally:
                send_move(0.0, 0.0)

        async def mission_spot(receipt, thing, jpeg):
            # A blocked provider must not hold the control loop or asyncio executor shutdown.
            # The daemon has no motion authority; its late result is discarded after cancellation.
            send_move(0.0, 0.0)
            result_queue = queue.Queue(maxsize=1)
            def infer():
                try:
                    result_queue.put((agent_mod.spot(thing, jpeg), None))
                except Exception as exc:
                    result_queue.put((None, type(exc).__name__))
            threading.Thread(target=infer, daemon=True, name="mission-spot").start()
            deadline = loop.time() + 15.0
            while True:
                mission_guard(receipt)
                if loop.time() >= deadline:
                    raise MissionInterrupted("vision timeout")
                try:
                    result, error = result_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    continue
                if error:
                    raise MissionInterrupted(f"vision failed: {error}")
                return result

        async def guarded_await(awaitable):
            """Keep the safety loop active while awaiting firmware, speech, or inference."""
            pending = asyncio.ensure_future(awaitable)
            try:
                while not pending.done():
                    mission_guard({"state": "executing"})
                    await asyncio.wait({pending}, timeout=0.05)
                mission_guard({"state": "executing"})
                return pending.result()
            finally:
                if not pending.done():
                    pending.cancel()

        async def ensure_standing():
            nonlocal needs_stand
            if not needs_stand:
                return
            for api in (STAND_UP, BALANCE_STAND):
                code = await guarded_await(request(api, timeout=4.0))
                if code not in (0, None):
                    raise MissionInterrupted(f"stand_failed:{code}")
                await guarded_await(asyncio.sleep(1.5))
            needs_stand = False

        async def show(trick_name, trick_api, trick_settle, text, look_up=False, on_speak=None):
            """Stop, speak, perform one firmware trick, let it finish, stand back up. Never raises on a slow ack."""
            nonlocal cmd_vx, last_show
            if no_motion:
                if text:
                    speak(text)
                    if on_speak is not None:
                        on_speak()
                last_show = loop.time()
                return "no_motion"
            send_move(0.0, 0.0)
            with contextlib.suppress(Exception):
                await guarded_await(request(STOP_MOVE, priority=True, timeout=3.0))
            if look_up and trick_api != EULER:  # stand to attention: lift the nose so the face is in frame
                with contextlib.suppress(Exception):
                    await guarded_await(request_params(EULER, {"x": 0.0, "y": LOOK_UP_PITCH, "z": 0.0}, timeout=3.0))
                    await guarded_await(asyncio.sleep(0.8))
            if text:
                speak(text)
                if on_speak is not None:
                    on_speak()
            try:
                code = await guarded_await(request(trick_api, timeout=8.0))
            except Exception:
                code = "no_ack"  # long behaviours (dance) do not ack within the window; the request was sent
            await guarded_await(asyncio.sleep(trick_settle))
            with contextlib.suppress(Exception):
                await guarded_await(request(BALANCE_STAND, timeout=3.0))
            await guarded_await(asyncio.sleep(1.0))
            cmd_vx = 0.0
            last_show = loop.time()
            stall.update(now_s=loop.time(), pose_xy=tel["pose"], commanded_vx=0.0)
            return code

        def brain_status():
            r = tel["ranges"] or {}
            return {"people": len(perception.latest()["tracks"]), "ranges": r, "home_m": math.dist(origin, tel["pose"]),
                    "leash_m": planner.leash_m, "battery": tel["soc"], "mode": mode, "greetings": len(report["greetings"])}

        def brain_think():
            """Runs in a thread: one VLM call, result stored for the control loop."""
            try:
                jpeg = view.jpeg if view.port else None
                now_s = loop.time()
                sightings = memory.summary(tel["pose"], tel["yaw"], now=now_s)
                if graph is not None:
                    with contextlib.suppress(Exception):
                        sightings = graph.summary(now=time.time(), limit=6).get("sentences", []) + sightings
                decision = brain.decide(jpeg, brain_status(), sightings[:10])
                brain_state["decision"], brain_state["last"] = decision, now_s
                brain_state["until"] = now_s + decision["seconds"]
                brain_state["count"] += 1
                if not decision.get("ok"):
                    report["brain"]["failures"] += 1
                report["brain"]["decisions"].append({"t_s": round(now_s - start, 1), **{k: decision[k] for k in
                                                     ("action", "seconds", "reason") if k in decision},
                                                     "latency_ms": decision.get("latency_ms")})
                report["brain"]["decisions"] = report["brain"]["decisions"][-50:]
                status(f"t={now_s-start:5.1f}s brain: {decision['action']} for {decision['seconds']:.0f}s "
                       f"({decision.get('reason', '')!r}, {decision.get('latency_ms', '?')} ms)")
            finally:
                brain_state["busy"] = False

        def on_voice(cmd, text):
            voice_state["count"] += 1
            report["voice"]["commands"].append({"t_s": round(loop.time() - start, 1), "intent": cmd["intent"],
                                                "text": text[:80]})
            if cmd["intent"] == "converse":  # Unaddressed conversation has no movement authority.
                sit = build_situation(time.time(), tracks=perception.latest().get("tracks", []), ranges=tel["ranges"],
                                      home_m=math.dist(origin, tel["pose"]) if tel["pose"] else 0.0, battery=tel["soc"], mode=mode)
                who = next((name for t in perception.latest().get("tracks", []) if (name := spoken_name(t.get("identity")))), None)
                rep = agent_mod.converse_reply(cmd["phrase"], sit, who=who, inference=chat_client)
                status(f"conversation: heard {cmd['phrase'][:80]!r} -> {rep['kind']}; saying {rep['text']!r} ({rep['source']})")
                report.setdefault("conversations", []).append({"t_s": round(loop.time() - start, 1), "name": who, "asked": None,
                                                               "heard": cmd["phrase"], "reply": rep["text"], "kind": rep["kind"]})
                if rep["kind"] == "concern":
                    report.setdefault("concerns", []).append({"t_s": round(loop.time() - start, 1), "t": time.time(), "name": who, "heard": cmd["phrase"][:200]})
                if rep["text"]:
                    speak(rep["text"])
                return
            if cmd["intent"] == "instruct":  # free speech after the wake word: the situated agent plans it
                code, receipt = missions.submit({"name": "instruct", "args": {"text": cmd["phrase"]}})
                status(f"voice instruction -> {code}: {cmd['phrase'][:80]!r}")
                speak("On it." if code == 202 else "One moment, I'm still busy.")
                return
            if cmd["intent"] == "stop":
                missions.submit({"name": "stop"})
            voice_state["resume_requested"] = cmd["intent"] != "stop"
            voice_state["intent"], voice_state["until"] = cmd["intent"], loop.time() + 12.0
            speak(f"Okay, {cmd['intent'].replace('_', ' ')}.")

        if voice is not None:
            voice.on_command = on_voice
            view.listener = voice
            global LISTENER
            LISTENER = voice
            voice.start()

        while loop.time() - start < duration_s:
            try:
                now = loop.time()
                report["elapsed_s"] = round(now - start, 1)
                if tel["soc"] < min_soc:
                    report["reason"] = "battery_low"
                    return report
                if now - tel["low_t"] > stale_s or now - tel["pose_t"] > stale_s:
                    report["reason"] = "telemetry_stale"
                    return report
                dist = math.dist(origin, tel["pose"])
                report["max_distance_from_origin_m"] = max(report["max_distance_from_origin_m"], round(dist, 3))
                if dist > boundary_m:
                    report["reason"] = "boundary_exceeded"
                    return report
                diag.tick("control")
                diag.tick("safety")
                res = perception.latest()
                if res["seq"] != last_seq:
                    last_seq = res["seq"]
                    tracks, fw, fh = res["tracks"], res["w"], res["h"]
                    report["frames"] = res["seq"]
                elif res["t"] is not None and time.monotonic() - res["t"] > 1.0:
                    tracks = []  # perception stalled: do not act on old boxes
                perception.set_context(mode="paused" if mission_paused else mode, ranges=tel["ranges"], battery=tel["soc"])
                if recorder is not None and now - rec_last[0] >= 0.2:
                    rec_last[0] = now
                    with contextlib.suppress(Exception):
                        wall = time.time()
                        recorder.record_pose(wall, tel["pose"][0], tel["pose"][1], tel["yaw"])
                        front_r = None if not ranges or ranges["front"] == float("inf") else ranges["front"]
                        if tracks and fw:
                            people_world = []
                            for t in tracks:
                                x1, y1, x2, y2 = t["box"]
                                cx = (x1 + x2) / 2.0 / float(fw)
                                hfrac = max(0.05, (y2 - y1) / float(fh or 480))
                                bearing = (0.5 - cx) * math.radians(100.0)  # Go2 front camera is roughly 100 deg wide
                                person_dist = front_r if (front_r is not None and abs(cx - 0.5) < 0.18) else min(6.0, 0.55 / hfrac)
                                ang = tel["yaw"] + bearing
                                ident = (t.get("identity") or {}).get("name")
                                people_world.append({"track_id": t.get("track_id"), "x": tel["pose"][0] + person_dist * math.cos(ang),
                                                     "y": tel["pose"][1] + person_dist * math.sin(ang), "z": 0.9,
                                                     "label": ident or f"person {t.get('track_id')}", "identity": ident,
                                                     "posture": t.get("posture")})
                        else:
                            people_world = []
                        objs = res.get("objects") or []
                        object_world = []
                        if objs and fw and objects_mod is not None and res.get("objects_seq", 0) != rec_objects_seq[0]:
                            rec_objects_seq[0] = res["objects_seq"]  # place each detection list once, at this pose
                            objs = objects_mod.not_people(objs, [t["box"] for t in res.get("raw_tracks", [])])  # a lying person is not a couch
                            objs = [o for o in objs if o.get("hits", 1) >= 2]  # drop one-frame hallucinations
                            placed = objects_mod.place(objs, fw, fh or 480, tel["pose"], tel["yaw"], front_range_m=front_r)
                            object_world = objects_mod.sightings(placed, int(wall * 1000))
                        last_people_world = people_world
                        if people_world or object_world:
                            recorder.record_people(wall, people_world + object_world)  # one call: it replaces "now"
                        if graph is not None:
                            with contextlib.suppress(Exception):
                                graph.ingest_pose(wall, tel["pose"][0], tel["pose"][1], tel["yaw"])
                                if people_world or object_world:
                                    graph.ingest_people(wall, [dict(p, kind="person") for p in people_world]
                                                        + [dict(o, kind="object") for o in object_world])
                if now - last_diag >= diag_every_s:
                    last_diag = now
                    status(f"diag {diag.line()}")
                    view.update(diag=diag.snapshot())
                ranges = tel["ranges"]
                if lidar and (tel["ranges_t"] is None or now - tel["ranges_t"] > lidar_stale_s):
                    ranges = None  # unknown: send_move rejects forward requests until a fresh map arrives
                    report["lidar"]["stale_ticks"] += 1
                map_age = None if tel["ranges_t"] is None else now - tel["ranges_t"]
                # camera guard: a detected object filling most of the frame height with its bottom near the frame's
                # bottom edge is right at the nose, whatever the LiDAR band says (chair seats, boxes, low tables)
                cam_block = None
                try:
                    latest_res = perception.latest()
                    fh_ = fh or 480
                    for o in latest_res.get("objects") or []:
                        x1, y1, x2, y2 = o["box"]
                        if (y2 - y1) > 0.55 * fh_ and y2 > 0.85 * fh_ and o.get("hits", 1) >= 2:
                            cam_block = o.get("label")
                            break
                except Exception:
                    cam_block = None
                if cam_block is not None and mode not in ("follow", "greet"):
                    ranges = dict(ranges or {"left": float("inf"), "right": float("inf")})
                    ranges["front"] = min(ranges.get("front", float("inf")), 0.4)
                    if now - guard_state.get("cam_log", 0.0) > 5.0:
                        guard_state["cam_log"] = now
                        status(f"t={now-start:5.1f}s camera guard: {cam_block} fills the view, treating the front as 0.4 m")
                front_now = None if not ranges or ranges["front"] == float("inf") else ranges["front"]
                current_places, current_radii = _person_places(tracks, fw or 640, fh or 480, tel["pose"], tel["yaw"], front_now)
                policy.observe(tracks, current_places, now_s=now)
                active_before_take = missions.executing()
                allow_social_policy = not mission_paused and (active_before_take is None or
                    (navigation_state.get("receipt") is active_before_take and navigation_state.get("name") == "patrol"))
                action, tid = policy.step(tracks, fw or 640, fh or 480, now_s=now, front_m=front_now,
                                          place_by_tid=current_places, radius_by_tid=current_radii) if fw and allow_social_policy else ("patrol", None)
                # A clothing selection is demo authorization, not a permanent identity.
                # Losing it cancels the current plan and requires a new explicit selection.
                selection = grandma_selection_status(view)
                selection_blocked = selection["enabled"] and selection["needs_selection"]
                if selection_blocked:
                    if not mission_paused:
                        send_move(0.0, 0.0)
                        missions.submit({"name": "stop"})
                        view.log("Movement held: select Grandma again (" + selection["state"] + ")")
                    mission_paused = True
                    report["paused"] = True
                    voice_state.update(intent=None, until=0.0, resume_requested=False)
                # ---- missions from the family app (body-command shape); they pre-empt greeting and idle tricks
                if missions.stop_requested:
                    mission_paused = True
                    report["paused"] = mission_paused
                    voice_state.update(intent=None, until=0.0, resume_requested=False)
                    instruct_state.update(receipt=None, plan=None, thread=None)
                    navigation_state.clear()
                    mission_hold_until[0] = 0.0
                    await process_stop_requests()
                    mission_state.clear()
                    status(f"t={now-start:5.1f}s mission stop: StopMove sent")
                if voice_state.pop("resume_requested", False) and not no_motion:
                    await ensure_standing()
                    mission_paused = False
                    report["paused"] = mission_paused
                new_mission = missions.take()
                if manual_control and new_mission is None and missions.executing() is None and instruct_state["receipt"] is None:
                    explicit_roaming = (voice_state.get("intent") in ("explore", "scan", "go_home", "follow", "approach",
                                                                   "sit", "stand", "dance", "hello", "heart", "stretch")
                                        and now < voice_state.get("until", 0))
                    if not explicit_roaming:
                        mission_paused = True
                        report["paused"] = True
                # Publish before hold/mission branches continue: pausing motion must not freeze observation.
                display_mode = "paused" if mission_paused else mode
                perception.set_context(mode=display_mode, ranges=ranges, battery=tel["soc"])
                view.update(mode=display_mode, action="hold" if mission_paused else action, paused=mission_paused,
                            tracks=tracks, ranges=ranges, battery=tel["soc"], t_s=now - start,
                            pose={"x": tel["pose"][0], "y": tel["pose"][1], "yaw": tel["yaw"]},
                            greetings=len(report["greetings"]), checkins=len(report["checkins"]), home_m=dist,
                            fps=diag.rate("processed"))
                if mission_paused and new_mission is None and missions.executing() is None and instruct_state["receipt"] is None:
                    send_move(0.0, 0.0)
                    await asyncio.sleep(tick)
                    continue
                if new_mission is not None:
                    name, margs = new_mission["name"], new_mission["args"]
                    wants_motion = name in ("turn", "walk", "look_for", "move", "patrol", "go_home", "hello", "dance", "heart", "stretch", "sit", "stand") or (name == "find_person" and margs.get("approach", False))
                    if selection_blocked and wants_motion:
                        missions.finish(new_mission, error="Select Grandma in the camera before moving: " + selection["state"])
                        mission_paused = True
                        report["paused"] = True
                        continue
                    if no_motion and wants_motion:
                        missions.finish(new_mission, error="no_motion: movement disabled")
                        mission_paused = True
                        report["paused"] = mission_paused
                        continue
                    if wants_motion:
                        await ensure_standing()
                        mission_paused = False
                        report["paused"] = mission_paused
                    now = loop.time()  # Preparation can take seconds; execution budgets begin after it.
                    status(f"t={now-start:5.1f}s mission {name} {margs if margs else ''}")
                    report["missions"].append({"t_s": round(now - start, 1), "name": name, "args": margs})
                    if name == "instruct":
                        sit = build_situation(time.time(), tracks=tracks, ranges=tel["ranges"], home_m=dist, battery=tel["soc"], mode="paused" if mission_paused else mode, max_age_s=0.0)
                        instruct_state = {"receipt": new_mission, "plan": None}
                        instruct_state["thread"] = threading.Thread(target=plan_instruct, args=(new_mission, sit, instruct_state), daemon=True, name="instruct")
                        instruct_state["thread"].start()
                        missions.progress(new_mission, step="planning")
                        continue
                    if name == "stop":
                        # A planned stop is a real stop barrier, not an unhandled child.
                        mission_paused = True
                        report["paused"] = True
                        voice_state["intent"], voice_state["until"] = None, 0.0
                        outcome = await process_stop_requests(force=True)
                        code = outcome["code"]
                        missions.finish(new_mission, result={"stop_code": code},
                                        error=outcome["error"] or (None if type(code) is int and code == 0 else "StopMove acknowledgment missing"))
                        continue
                    if name in ("turn", "walk", "look_for"):
                        try:
                            mission_guard(new_mission, moving=True)
                            if name == "turn":
                                err = await mission_turn(new_mission, margs["degrees"],
                                                         min(10.0, abs(margs["degrees"]) / 30.0 + 2.0))
                                result = {"turned_deg": round(margs["degrees"] - err, 1), "remaining_deg": round(err, 1)}
                            elif name == "walk":
                                gone = await mission_walk(new_mission, margs["metres"], abs(margs["metres"]) / 0.2 + 2.0)
                                result = {"walked_m": round(gone, 2), "stopped_by": None}
                            else:
                                thing, seen_at, answers = margs["thing"], None, []
                                for stop in range(6):
                                    ans = await mission_spot(new_mission, thing, view.jpeg if view.port else None)
                                    answers.append({"stop": stop, **{k: ans.get(k) for k in ("seen", "where", "latency_ms", "error")}})
                                    if ans.get("error"):
                                        raise MissionInterrupted("vision failed")
                                    if ans.get("seen"):
                                        seen_at = ans
                                        break
                                    if stop < 5:
                                        await mission_turn(new_mission, 60.0, 4.0)
                                        await mission_wait(new_mission, 0.4)
                                if seen_at is None:
                                    raise MissionInterrupted("target not found")
                                nudge = {"left": 35.0, "centre": 0.0, "right": -35.0}.get(seen_at.get("where"))
                                if nudge is None:
                                    raise MissionInterrupted("vision bearing unavailable")
                                if nudge:
                                    await mission_turn(new_mission, nudge, 3.0)
                                # A generic range hit cannot establish that the requested object was reached.
                                # Report the bounded approach separately from the visual detection.
                                gone = await mission_walk(new_mission, 2.5, 12.0)
                                missions.progress(new_mission, found=True, thing=thing, where=seen_at.get("where"),
                                                  walked_m=round(gone, 2), approached=False, stops=answers)
                                raise MissionInterrupted("approach limit reached: target arrival unverified")
                            mission_guard(new_mission)
                            missions.finish(new_mission, result=result)
                        except MissionInterrupted as exc:
                            mission_paused = True
                            report["paused"] = mission_paused
                            voice_state["resume_requested"] = False
                            missions.finish(new_mission, error=str(exc))
                            await process_stop_requests(force=True)
                            if str(exc) in ("duration_complete", "battery_low", "telemetry_stale", "boundary_exceeded"):
                                report["reason"] = str(exc)
                                return report
                        continue
                    if name == "dance" and tel["ranges"] and tel["ranges"].get("front", float("inf")) < 0.5:
                        missions.finish(new_mission, error=f"too close to something ({tel['ranges']['front']:.2f} m ahead) to dance safely")
                        continue
                    if name in ("hello", "dance", "heart", "stretch", "sit", "stand"):
                        api = {"sit": 1009, "stand": STAND_UP, "dance": 1022, "hello": HELLO, "heart": 1036, "stretch": 1017}[name]
                        settle = {"sit": 3.0, "stand": 2.0, "dance": 10.0, "hello": 4.0, "heart": 6.0, "stretch": 5.0}[name]
                        if no_motion:
                            missions.finish(new_mission, error="no_motion: movement disabled")
                            continue
                        code = await show(name, api, settle, None)
                        missions.finish(new_mission, result={"codes": {name: code}, "note": "firmware acknowledgment, not evidence the motion happened"},
                                        error=None if type(code) is int and code == 0 else f"{name} not acknowledged: {code}")
                        continue
                    if name == "say":
                        send_move(0.0, 0.0)
                        played = await guarded_await(loop.run_in_executor(None, lambda: blocking_speak(margs["text"])))
                        missions.finish(new_mission, result={"played": bool(played), "text": margs["text"], "where": audio_where or "host speaker", "source": "simulation" if audio_where else source},
                                        error=None if played else "speech playback failed")
                        continue
                    if name == "listen":
                        send_move(0.0, 0.0)
                        try:
                            heard = await guarded_await(loop.run_in_executor(None, lambda: blocking_listen(margs["max_s"])))
                            missions.finish(new_mission, result={**heard, "where": audio_where or "host microphone", "source": "simulation" if audio_where else source})
                        except Exception as exc:
                            missions.finish(new_mission, error=f"listen failed: {type(exc).__name__}")
                        continue
                    if name == "move":
                        missions.finish(new_mission, error="move is not offered by the patrol process; use patrol/go_home")
                        continue
                    if name == "patrol":
                        mission_hold_until[0] = 0.0
                        voice_state["intent"], voice_state["until"] = "explore", now + margs["duration_s"]
                        navigation_state.update(receipt=new_mission, name=name, deadline=now + margs["duration_s"], since=now)
                        missions.progress(new_mission, step="exploring", duration_s=margs["duration_s"])
                        continue
                    if name == "go_home":
                        mission_hold_until[0] = 0.0
                        voice_state["intent"], voice_state["until"], voice_state["source"] = "go_home", now + 90.0, "mission"
                        navigation_state.update(receipt=new_mission, name=name, deadline=now + 90.0, since=now)
                        missions.progress(new_mission, step="returning home", home_m=round(dist, 2))
                        continue
                    if name == "find_person":
                        mission_state.update(receipt=new_mission, name=(margs["name"] or "").lower() or None,
                                             deadline=now + margs["timeout_s"], approach=margs["approach"], found=None, since=now, identities={})
                if instruct_state["receipt"] is not None and instruct_state["plan"] is None:
                    send_move(0.0, 0.0)
                    await asyncio.sleep(tick)
                    continue
                if instruct_state["receipt"] is not None and instruct_state["plan"] is not None:
                    rec, plan = instruct_state["receipt"], instruct_state["plan"]
                    instruct_state.update(receipt=None, plan=None, thread=None)
                    status(f"t={now-start:5.1f}s instruct ({plan.get('source')}, {plan.get('latency_ms')} ms): {len(plan['steps'])} steps "
                           f"{[st['name'] for st in plan['steps']]}; reply {plan.get('reply')!r}"
                           + (f"; rejected {plan['rejected']}" if plan.get("rejected") else ""))
                    report.setdefault("instructions", []).append({"t_s": round(now - start, 1), "text": rec["args"]["text"], "source": plan.get("source"),
                                                                  "steps": [st["name"] for st in plan["steps"]], "reply": plan.get("reply")})
                    if plan.get("reply") and not any(step["name"] == "say" and step.get("args", {}).get("text") == plan["reply"]
                                                     for step in plan["steps"]):
                        speak(plan["reply"])
                    missions.chain(rec, plan["steps"], reply=plan.get("reply", ""), source=plan.get("source", ""))
                    send_move(0.0, 0.0)
                    await asyncio.sleep(tick)
                    continue
                mission = missions.executing()
                if mission is not None and navigation_state.get("receipt") is mission:
                    going_home = navigation_state["name"] == "go_home"
                    reached = going_home and dist < 0.4
                    expired = now >= navigation_state["deadline"]
                    if reached or expired:
                        send_move(0.0, 0.0)
                        missions.finish(mission, result={"home_m": round(dist, 2), "elapsed_s": round(now - navigation_state["since"], 2)},
                                        error="home timeout: origin not reached" if going_home and not reached else None)
                        navigation_state.clear()
                        voice_state["intent"], voice_state["until"] = None, 0.0
                        if manual_control:
                            mission_paused = True
                            report["paused"] = True
                        continue
                    if going_home:
                        action, tid = "patrol", None
                if mission is not None and mission_state.get("receipt") is mission:
                    if now > mission_state["deadline"]:
                        missions.finish(mission, error="find_person timeout: target not reached", result={"found": False, "track_id": None, "identity": None, "matched_name": False,
                                                         "posture": None, "approached": False,
                                                         "searched_s": round(now - mission_state["since"], 1)})
                        mission_state.clear()
                        continue
                    wanted = mission_state["name"]
                    pool = _mission_person_pool(tracks, wanted, mission_state["identities"], now)
                    if pool:
                        best = max(pool, key=lambda t: t["box"][3] - t["box"][1])
                        found = {"track_id": best.get("track_id"), "identity": best.get("identity"),
                                 "matched": bool(wanted), "posture": best.get("posture")}
                        if found != mission_state["found"]:
                            missions.progress(mission, found=found)
                            status(f"t={now-start:5.1f}s mission find_person: found track {best.get('track_id')}")
                        mission_state["found"] = found
                        # "approached" must be reachable by the follow controller: it holds at box height 0.5 / LiDAR 0.6 m
                        approached = (not mission_state["approach"]) or (front_now is not None and front_now <= 0.75) \
                            or (best["box"][3] - best["box"][1]) / float(fh or 480) >= 0.47
                        if approached:
                            f = mission_state["found"]
                            missions.finish(mission, result={"found": True, "track_id": f["track_id"], "identity": f["identity"],
                                                             "matched_name": f["matched"], "posture": f["posture"], "approached": bool(mission_state["approach"]),
                                                             "searched_s": round(now - mission_state["since"], 1)})
                            send_move(0.0, 0.0)
                            policy.greeted[f["track_id"]] = now  # the errand does the talking: no "hello there" on top
                            mission_hold_until[0] = now + 60.0  # and keep following them quietly while the errand speaks/listens
                            mission_state.clear()
                            mode = "cruise"
                            await asyncio.sleep(tick)
                            continue
                        action, tid = "follow", best.get("track_id")  # walk up to them; the follow branch below does the driving
                    else:
                        action, tid = "patrol", None  # keep searching (bandit + brain pull toward the last sighting)
                if mission_paused:
                    send_move(0.0, 0.0)
                    await asyncio.sleep(tick)
                    continue
                social_patrol = (mission is not None and navigation_state.get("receipt") is mission
                                 and navigation_state.get("name") == "patrol")
                if ((mission is not None and not social_patrol) or now < mission_hold_until[0]) and action in ("greet", "checkin"):
                    action = "follow" if tid is not None else "patrol"  # no tricks or questions while on / just after a mission
                if now < mission_hold_until[0] and mission is None:
                    send_move(0.0, 0.0)  # Hold between the errand's approach, speech and listening commands.
                    await asyncio.sleep(tick)
                    continue
                if action in ("follow", "greet") and not bandit_state["rewarded"] and bandit.current is not None:
                    bandit.reward(1.0)  # this heading found someone
                    bandit_state["rewarded"] = True
                if action == "greet" and memory.greeted_here_recently(tel["pose"], tel["yaw"], now=now, within_s=300.0):
                    action = "follow"  # same spot, same heading, moments ago: that is the person we already greeted
                if tracks and now - memory_last_seen[0] > 5.0:
                    memory_last_seen[0] = now
                    named = next(((t.get("identity") or {}).get("name") for t in tracks if t.get("identity")), None)
                    memory.add(t=now, pose=tel["pose"], yaw=tel["yaw"], kind="seen", people=len(tracks), note=named)
                    grid["map"].mark(tel["pose"][0], tel["pose"][1], "seen")
                # voice commands: a bounded override of what the dog is doing (guardrails still apply below)
                intent = voice_state["intent"] if now < voice_state["until"] else None
                if intent in ("sit", "stand", "dance", "hello", "heart", "stretch"):
                    voice_state["intent"] = None
                    api = {"sit": 1009, "stand": STAND_UP, "dance": 1022, "hello": HELLO, "heart": 1036, "stretch": 1017}[intent]
                    settle = {"sit": 3.0, "stand": 2.0, "dance": 10.0, "hello": 4.0, "heart": 6.0, "stretch": 5.0}[intent]
                    status(f"t={now-start:5.1f}s voice: {intent}")
                    await show(intent, api, settle, None)
                    continue
                if intent == "stop":
                    send_move(0.0, 0.0)
                    mode = "voice_stop"
                    await asyncio.sleep(tick)
                    continue
                if intent in ("follow", "approach") and action == "patrol":
                    action = "patrol"  # nobody visible: keep looking (brain/scan), the follow branch takes over when seen
                if brain is not None and action == "patrol" and not brain_state["busy"] \
                        and (brain_state["last"] is None or now - brain_state["last"] >= brain_period_s):
                    brain_state["busy"] = True
                    threading.Thread(target=brain_think, daemon=True, name="brain").start()
                if action == "patrol" and mission is None and now - last_remark_check >= 5.0 and voice is not None:
                    last_remark_check = now
                    wall = time.time()
                    sit = build_situation(wall, tracks=tracks, ranges=tel["ranges"], home_m=dist, battery=tel["soc"], mode=mode)
                    remark = narrator.remark(sit, now=wall)
                    if remark:
                        send_move(0.0, 0.0)
                        line = await guarded_await(loop.run_in_executor(None, lambda: agent_mod.compose_line(
                            "remark on something you just noticed while exploring", sit, inference=chat_client, fallback=remark, extra=f"Noticed: {remark}")))
                        remark = line["text"]
                        status(f"t={now-start:5.1f}s remark ({line['source']}): {remark!r}")
                        report.setdefault("remarks", []).append({"t_s": round(now - start, 1), "text": remark})
                        memory.add(t=now, pose=tel["pose"], yaw=tel["yaw"], kind="remark", people=len(tracks), note=remark[:60])
                        speak(remark)
                if action == "greet":
                    track = next(t for t in tracks if t.get("track_id") == tid)
                    wall = time.time()
                    world = next(((p["x"], p["y"]) for p in (last_people_world or []) if p.get("track_id") == tid), None)
                    if world is None and tel["pose"] is not None:
                        fr = (tel["ranges"] or {}).get("front")
                        d0 = fr if fr is not None and fr != float("inf") else 1.0
                        world = (tel["pose"][0] + d0 * math.cos(tel["yaw"]), tel["pose"][1] + d0 * math.sin(tel["yaw"]))
                    sit = build_situation(wall, tracks=tracks, ranges=tel["ranges"], home_m=dist, battery=tel["soc"], mode=mode)
                    # ANNIE_GREET_REPEAT_S: how long a name or a spot counts as "already greeted" (300 s at home;
                    # a crowd demo wants it shorter so the dog keeps saying hi)
                    decision = agent_mod.greeting_decision(dict(track, world=world), sit, now=wall,
                                                           recent_s=float(os.environ.get("ANNIE_GREET_REPEAT_S", "300")))
                    if not decision["greet"]:
                        policy.greeted[tid] = now  # known already: no repeat greeting, keep exploring
                        status(f"t={now-start:5.1f}s person (track {tid}) ahead: not greeting again ({decision['reason']})")
                        continue
                    who = spoken_name(track.get("identity"))
                    send_move(0.0, 0.0)
                    line = await guarded_await(loop.run_in_executor(None, lambda: agent_mod.compose_line(
                        f"greet {who or 'this person'} who is right in front of you", sit, inference=chat_client, fallback=decision["text"],
                        extra=f"Person in front: {who or 'someone you do not know by name'}; nearby objects: "
                              f"{', '.join(o['name'] for o in sit['objects'][:3]) or 'none known'}.")))
                    text = line["text"]
                    trick_name, trick_api, trick_settle = GREET_TRICKS[len(report["greetings"]) % len(GREET_TRICKS)]
                    status(f"t={now-start:5.1f}s person (track {tid}) ahead: stopping, {trick_name}, saying {text!r} "
                           f"({decision['reason']}; line by {line['source']}, {line['latency_ms']} ms)")
                    memory.add(t=now, pose=tel["pose"], yaw=tel["yaw"], kind="greet", people=len(tracks),
                               note=((track.get("identity") or {}).get("name")) or trick_name)
                    grid["map"].mark(tel["pose"][0], tel["pose"][1], "greet")
                    code = await show(trick_name, trick_api, trick_settle, text, look_up=True)
                    # A conversation turn: the greeting asked how they are; listen, answer in character, escalate concern.
                    heard_text, reply_text, reply_kind = None, None, "none"
                    if (voice is not None or audio is not None) and not no_motion:
                        try:
                            heard = await guarded_await(loop.run_in_executor(None, lambda: blocking_listen(6.0)))
                            heard_text = heard.get("transcript")
                        except Exception:
                            heard_text = None
                        if heard_text:
                            rep = await guarded_await(loop.run_in_executor(None, lambda: agent_mod.converse_reply(heard_text, sit, who=who, inference=chat_client)))
                            reply_text, reply_kind = rep["text"], rep["kind"]
                            status(f"t={now-start:5.1f}s heard {who or 'them'}: {heard_text[:80]!r} -> {reply_kind}; saying {reply_text!r} ({rep['source']})")
                            if reply_text:
                                await guarded_await(loop.run_in_executor(None, lambda: blocking_speak(reply_text)))
                            if reply_kind == "concern":
                                report.setdefault("concerns", []).append({"t_s": round(now - start, 1), "t": wall, "name": who, "heard": heard_text[:200]})
                                memory.add(t=now, pose=tel["pose"], yaw=tel["yaw"], kind="concern", people=len(tracks), note=(who or "person") + ": " + heard_text[:50])
                                with contextlib.suppress(Exception):
                                    if graph is not None:
                                        graph.ingest_event(wall, tel["pose"][0], tel["pose"][1], "concern", heard_text[:120])
                    report.setdefault("conversations", []).append({"t_s": round(now - start, 1), "name": who, "asked": text, "heard": heard_text,
                                                                   "reply": reply_text, "kind": reply_kind, "source": "simulation" if audio_where else source, "mocked_audio": bool(audio_where)})
                    report["greetings"].append({"t_s": round(now - start, 1), "t": wall, "track_id": tid, "text": text,
                                                "trick": trick_name, "hello_code": code, "identity": track.get("identity"),
                                                "name": (track.get("identity") or {}).get("name"),
                                                "x": None if world is None else round(world[0], 2), "y": None if world is None else round(world[1], 2)})
                    continue
                if action == "checkin":
                    # A lying person is a posture estimate, not a diagnosis: stop, ask, wave, and keep an eye out.
                    track = next(t for t in tracks if t.get("track_id") == tid)
                    mission_guard({"state": "executing"})
                    status(f"t={now-start:5.1f}s person (track {tid}) appears to be lying down: stopping and asking")
                    memory.add(t=now, pose=tel["pose"], yaw=tel["yaw"], kind="checkin", people=len(tracks))
                    code = await show("hello", HELLO, 4.0, "Hey, are you alright? Please say okay or help.",
                                      on_speak=lambda: policy.mark_checkin(track, current_places.get(tid),
                                                                         now_s=loop.time(), radius_m=current_radii.get(tid)))
                    report["checkins"].append({"t_s": round(now - start, 1), "track_id": tid, "hello_code": code})
                    if stop_on_checkin:
                        report["reason"] = "checkin_raised"
                        return report  # hand over to the incident flow
                    continue
                if action == "follow":
                    track = next(t for t in tracks if t.get("track_id") == tid)
                    vx, wz, reason = follow_command(track["box"], fw or 640, fh or 480, target_height_frac=0.5,
                                                    too_close_frac=0.9, max_vx=0.5, max_wz=1.0, k_yaw=2.2, k_dist=1.8)
                    cx = (track["box"][0] + track["box"][2]) / 2.0 / float(fw or 640)
                    centred = abs(cx - 0.5) <= 0.18
                    front_m = None if not ranges or ranges["front"] == float("inf") else ranges["front"]
                    if centred and front_m is not None:
                        # depth from the LiDAR beats box size: fast while far, ease off with distance, hold at ~0.75 m
                        if front_m < 0.6:
                            vx, reason = 0.0, "lidar_close"
                        elif reason != "too_close":
                            vx = min(0.5, max(0.2, 0.4 * (front_m - 0.6)))  # 2 m -> 0.5, 1.1 m -> 0.2
                            reason = "lidar_far" if front_m > 1.1 else "lidar_near"
                    if 0.0 < vx < 0.2:
                        vx = 0.2  # walking deadband: either walk properly or hold
                    if ranges and ranges["front"] < 0.4 and reason != "lidar_far":
                        vx = min(vx, 0.0)
                    vx, wz = guarded_velocity(vx, wz)
                    last_seen.update(tid=tid, t=now, side=1.0 if cx < 0.5 else -1.0)
                    holding = reason in ("centered", "too_close") or abs(vx) < 0.05
                    if holding and follow_hold["tid"] == tid and follow_hold["since"] is not None:
                        if now - follow_hold["since"] > follow_hold_s and tid in policy.greeted:
                            policy.ignore(tid, now_s=now)
                            status(f"t={now-start:5.1f}s track {tid} is standing still and already greeted: resuming patrol")
                            follow_hold.update(tid=None, since=None)
                            mode = "cruise"
                            send_move(0.0, 0.0)
                            await asyncio.sleep(tick)
                            continue
                    elif holding:
                        follow_hold.update(tid=tid, since=now)
                    else:
                        follow_hold.update(tid=tid, since=None)
                    stalled = stall.update(now_s=now, pose_xy=tel["pose"], commanded_vx=max(vx, 0.0))
                    if stalled:
                        report["collisions"].append({"t_s": round(now - start, 1), "mode": "follow", "front_m": None})
                        status(f"t={now-start:5.1f}s COLLISION while following: stopping")
                        vx = 0.0
                    cmd_vx = max(vx, 0.0)
                    if mode != "follow":
                        status(f"t={now-start:5.1f}s mode {mode}->follow (track {tid}, {reason})")
                        mode = "follow"
                    report["modes"]["follow"] = report["modes"].get("follow", 0) + 1
                    send_move(vx, wz)
                    if now - last_print >= 2.0:
                        last_print = now
                        status(f"t={now-start:5.1f}s follow track {tid} v={vx:+.2f} w={wz:+.2f} ({reason}) "
                               f"people={len(tracks)} battery={tel['soc']:.0f}%")
                    await asyncio.sleep(tick)
                    continue
                if mode == "follow" and last_seen["t"] is not None and now - last_seen["t"] < 2.0 and not tracks:
                    # lost them a moment ago: turn toward where they went instead of wandering off
                    wz = 0.7 * last_seen["side"]
                    cmd_vx = 0.0
                    send_move(0.0, wz)
                    report["modes"]["search"] = report["modes"].get("search", 0) + 1
                    if now - last_print >= 2.0:
                        last_print = now
                        status(f"t={now-start:5.1f}s search: lost track {last_seen['tid']}, turning {'left' if wz > 0 else 'right'}")
                    await asyncio.sleep(tick)
                    continue
                if mode == "follow":
                    mode = "cruise"  # lost them for good: back to the wander planner
                if idle_trick_s and mission is None and mode.startswith(("cruise", "brain:")) and now - last_show >= idle_trick_s:
                    trick_name, trick_api, trick_settle = GREET_TRICKS[(len(report["greetings"]) + len(report["checkins"])) % len(GREET_TRICKS)]
                    status(f"t={now-start:5.1f}s nobody new for {idle_trick_s:.0f}s: {trick_name} for fun")
                    report.setdefault("idle_tricks", []).append({"t_s": round(now - start, 1), "trick": trick_name,
                                                                 "code": await show(trick_name, trick_api, trick_settle, None)})
                    continue
                stalled = stall.update(now_s=now, pose_xy=tel["pose"], commanded_vx=cmd_vx)
                if stalled:
                    report["collisions"].append({"t_s": round(now - start, 1), "mode": mode,
                                                 "front_m": None if not ranges else round(min(ranges["front"], 99.0), 2)})
                    front = "?" if not ranges else f"{min(ranges['front'], 99.0):.2f}"
                    status(f"t={now-start:5.1f}s COLLISION: no progress while driving (front={front} m); "
                           "backing off and turning")
                if now >= bandit_state["until"]:  # pick the next heading to explore (UCB over sectors)
                    def prior(a, _g=grid["map"], _p=tel["pose"]):
                        free = _g.free_ahead(_p[0], _p[1], bandit.sector_heading(a))
                        if free < 0.7:
                            return -1.0  # known wall/desk that way
                        bonus = 0.0
                        if identifier is not None:  # pull toward where the named target was last seen but not yet greeted
                            seen = memory.last_seen_named(identifier.name, now=now)
                            if seen and not seen["greeted"]:
                                heading_to = math.atan2(seen["y"] - _p[1], seen["x"] - _p[0])
                                if abs(wrap_angle(heading_to - bandit.sector_heading(a))) < math.pi / bandit.n_arms:
                                    bonus = 1.0
                        goal = frontier["goal"]
                        if goal is not None:  # dimOS frontier exploration: prefer the sector that points at the next frontier
                            to_goal = math.atan2(goal[1] - _p[1], goal[0] - _p[0])
                            if abs(wrap_angle(to_goal - bandit.sector_heading(a))) < math.pi / bandit.n_arms:
                                bonus += 0.8
                        return bonus + 0.3 * min(free, 3.0) / 3.0 + 0.5 * _g.unvisited_ahead(_p[0], _p[1], bandit.sector_heading(a))
                    if frontier["planner"] is not None and now - frontier["t"] > 8.0:
                        frontier["t"] = now
                        try:  # ~0.4 s of pure Python: run it off the loop
                            goal = await guarded_await(loop.run_in_executor(None, lambda: frontier["planner"].next_goal(
                                grid["map"], tel["pose"], tel["yaw"], clear_range_m=1.5)))
                            frontier["goal"] = goal
                            if goal is not None:
                                report["frontier"]["goals"] += 1
                                status(f"t={now-start:5.1f}s frontier (dimOS): next goal {goal[0]:.1f},{goal[1]:.1f}")
                        except Exception as exc:
                            frontier["goal"] = None
                            report["frontier"]["error"] = type(exc).__name__
                    arm = bandit.choose(prior=prior)
                    planner.target_heading = bandit.sector_heading(arm)
                    bandit_state.update(until=now + 12.0, rewarded=False)
                    report["bandit"] = bandit.snapshot()
                    status(f"t={now-start:5.1f}s bandit: explore heading sector {arm} ({math.degrees(planner.target_heading):+.0f} deg)"
                           f" values={[round(v, 1) for v in bandit.value]}")
                cmd_vx, cmd_wz, new_mode = planner.step(now_s=now, ranges=ranges, pose_xy=tel["pose"], yaw=tel["yaw"],
                                                        origin_xy=origin, stalled=stalled)
                if cmd_vx > 0 and (map_age is None or map_age > 0.8):  # slow aging maps; send_move holds once stale
                    cmd_vx = min(cmd_vx, 0.2)
                if new_mode in ("blocked", "backoff") and not bandit_state["rewarded"]:
                    bandit.reward(0.0)  # this heading is a wall
                    bandit_state.update(rewarded=True, until=now + 1.0)  # re-choose soon after clearing
                    blocked_times.append(now)
                    blocked_times[:] = [t for t in blocked_times if now - t < 20.0]
                    if len(blocked_times) >= 3:  # stuck against something (wall, desk): escape toward the freest known direction
                        g = grid["map"]
                        best = max(range(bandit.n_arms), key=lambda a: g.free_ahead(tel["pose"][0], tel["pose"][1], bandit.sector_heading(a)))
                        planner.target_heading = bandit.sector_heading(best)
                        bandit.current = best
                        bandit_state.update(until=now + 12.0, rewarded=False)
                        blocked_times.clear()
                        memory.add(t=now, pose=tel["pose"], yaw=tel["yaw"], kind="collision", note="stuck")
                        status(f"t={now-start:5.1f}s stuck: 3 blocks in 20 s, escaping toward sector {best} "
                               f"({math.degrees(planner.target_heading):+.0f} deg, {g.free_ahead(tel['pose'][0], tel['pose'][1], planner.target_heading):.1f} m free)")
                decision = brain_state["decision"] if brain is not None and now < brain_state["until"] else None
                if intent in ("scan", "explore", "go_home") and now < voice_state["until"]:
                    decision = {"action": intent, "ok": True}
                if decision and new_mode == "cruise":  # guardrail modes (blocked/backoff/homing) always win
                    act = decision["action"]
                    if act in ("turn_left", "turn_right", "scan"):
                        cmd_vx, cmd_wz = 0.0, (planner.turn_rps if act != "turn_right" else -planner.turn_rps)
                    elif act == "wait":
                        cmd_vx, cmd_wz = 0.0, 0.0
                    elif act == "go_home":
                        err = wrap_angle(math.atan2(origin[1] - tel["pose"][1], origin[0] - tel["pose"][0]) - tel["yaw"])
                        cmd_wz = max(-planner.turn_rps, min(planner.turn_rps, 1.2 * err))
                        cmd_vx = planner.cruise_mps if abs(err) < math.pi / 3 else 0.0
                        if dist < 0.4:
                            cmd_vx, cmd_wz = 0.0, 0.0
                    # explore / approach: the planner's cruise velocities (already obstacle-aware)
                    new_mode = f"brain:{act}"
                if new_mode != mode:
                    front = "?" if not ranges else f"{min(ranges['front'], 99.0):.2f}"
                    status(f"t={now-start:5.1f}s mode {mode}->{new_mode} (front={front} m, home={dist:.2f} m)")
                    mode = new_mode
                report["modes"][mode] = report["modes"].get(mode, 0) + 1
                send_move(cmd_vx, cmd_wz)
                if now - last_print >= 2.0:
                    last_print = now
                    front = "?" if not ranges else f"{min(ranges['front'], 99.0):.2f}"
                    status(f"t={now-start:5.1f}s {mode} v={cmd_vx:+.2f} w={cmd_wz:+.2f} front={front} m "
                           f"home={dist:.2f} m people={len(tracks)} battery={tel['soc']:.0f}%")
                await asyncio.sleep(tick)
            except MissionInterrupted as exc:
                if str(exc) != "stopped by operator":
                    raise
                # Abandon this action without a completion receipt. Keep the link and
                # HTTP service alive; only a subsequent operator command releases hold.
                mission_paused = True
                report["paused"] = mission_paused
                voice_state["resume_requested"] = False
                voice_state["intent"], voice_state["until"] = None, 0.0
                mission_state.clear()
                navigation_state.clear()
                instruct_state.update(receipt=None, plan=None, thread=None)
                await process_stop_requests(force=True)
                continue
        report["reason"] = "duration_complete"
    except MissionInterrupted as exc:
        report["reason"] = "operator_cancelled" if str(exc) == "stopped by operator" else str(exc)
    except asyncio.CancelledError:
        report["reason"] = "operator_cancelled"
    except Exception as exc:
        report["reason"] = f"error:{type(exc).__name__}"
        report["error"] = safe_error(exc)
    finally:
        active_mission = missions.executing()
        if active_mission is not None:
            missions.finish(active_mission, error=f"patrol ended: {report['reason']}")
        if conn is not None and report["connection"]["status"] == "connected":
            with contextlib.suppress(Exception):
                send_move(0.0, 0.0)
        stopped = True
        perception.stop()
        if voice is not None:
            voice.stop()
        view.stop()
        report["diag"] = diag.snapshot()
        if graph is not None:
            with contextlib.suppress(Exception):
                report["graph"] = {"places": len(graph.places()), "summary": graph.summary(now=time.time(), limit=6).get("sentences", [])}
        if conn is not None and report["connection"]["status"] == "connected":
            if commanded or missions.stop_requested:
                report["stop"]["requested"] = True
                outcome = await process_stop_requests(force=True)
                report["stop"]["code"], report["stop"]["ack_ms"] = outcome["code"], outcome["ack_ms"]
            with contextlib.suppress(Exception):
                conn.datachannel.switchVideoChannel(False)
            try:
                await asyncio.wait_for(conn.disconnect(), 5)
                report["connection"]["disconnected"] = True
            except Exception as exc:
                report["connection"]["disconnected"] = False
        remaining_stops = missions.take_stops()
        if remaining_stops:
            missions.finish_stops(remaining_stops, code=None, ack_ms=None, error="Connection closed before StopMove acknowledgment")
        report["completed"] = report["reason"] in ("duration_complete", "operator_cancelled", "checkin_raised") \
            and report["connection"].get("disconnected", False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Patrol a slow circle and greet people (supervised).")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--speed", type=float, default=0.35, help="m/s, at most 0.45")
    parser.add_argument("--boundary", type=float, default=5.0, help="metres from the start; the leash is 80% of it")
    parser.add_argument("--no-lidar", action="store_true", help="skip the voxel-map sector ranges (stall detection only)")
    parser.add_argument("--firmware-avoid", action="store_true", help="also switch on the firmware obstacle-avoid service")
    parser.add_argument("--stop-on-checkin", action="store_true", help="end the run after the first lying-person check-in")
    parser.add_argument("--idle-trick", type=float, default=0.0, help="seconds without a new person before a trick; 0 (default) = keep exploring")
    parser.add_argument("--view-port", type=int, default=8011, help="live camera/boxes page; 0 disables")
    parser.add_argument("--view-host", default="127.0.0.1", help="bind address; beyond loopback requires ANNIE_BODY_TOKEN "
                        "and ANNIE_VIEW_HOSTS (comma list of this machine's addresses the page may be opened on)")
    parser.add_argument("--start-paused", "--manual-on-demand", action="store_true", help="wait for an explicit command; do not stand or patrol at startup")
    parser.add_argument("--manual-control", action="store_true", help="start held and stay held between explicit missions; no automatic roaming or greeting")
    parser.add_argument("--require-grandma-selection", action="store_true", help="hold motion until the operator selects Grandma's live clothing appearance; hold again if lost or ambiguous")
    parser.add_argument("--demo-everyone-grandma", action="store_true", help="staged demo only: treat every visible person as Grandma; starts held in manual control")
    parser.add_argument("--autonomous-demo", action="store_true", help="with --demo-everyone-grandma, explicitly allow idle patrol, greetings and occasional gestures; Pause still holds")
    parser.add_argument("--no-motion", action="store_true", help="perception-only: never move or perform tricks")
    parser.add_argument("--imgsz", type=int, default=320, help="tracker inference size (multiple of 32); 320 keeps up with the 14 fps stream on this Mac")
    parser.add_argument("--diag-every", type=float, default=5.0, help="seconds between diagnostic lines")
    parser.add_argument("--brain", action="store_true", help="let the local VLM propose patrol moves (guardrails stay)")
    parser.add_argument("--brain-period", type=float, default=4.0)
    parser.add_argument("--voice", action="store_true", help="listen for 'Annie, ...' commands on the host mic")
    parser.add_argument("--memory-file", default=".data/hardware/sightings.jsonl")
    parser.add_argument("--spacetime-file", default=".data/hardware/spacetime.jsonl")
    parser.add_argument("--target", default=os.environ.get("ANNIE_TARGET", "Jeanine:red"),
                        help="NAME:COLOUR of the person to recognise by shirt colour; empty disables")
    parser.add_argument("--output")
    parser.add_argument("--sim", nargs="?", const="seated", choices=("seated", "floor", "empty"), default=None,
                        help="no robot: drive the simulated dog in the MuJoCo apartment (docs/SIM_DOG.md); report source = simulation")
    parser.add_argument("--mock-audio", action="store_true", help="simulation only: scripted audio, no mic/speaker/providers")
    parser.add_argument("--mock-transcript", default="I'm doing well, thank you.")
    args = parser.parse_args(argv)
    if args.require_grandma_selection and args.demo_everyone_grandma:
        parser.error("Choose either --require-grandma-selection or --demo-everyone-grandma")
    if args.autonomous_demo and (not args.demo_everyone_grandma or args.manual_control or args.start_paused):
        parser.error("--autonomous-demo requires --demo-everyone-grandma without manual or paused startup")
    if args.require_grandma_selection or args.demo_everyone_grandma:
        args.target = ""
        if not args.autonomous_demo:
            args.manual_control, args.start_paused = True, True
    if args.mock_audio and (not args.sim or args.voice):
        parser.error("--mock-audio requires --sim and cannot be combined with --voice")
    mock_audio = None
    if args.mock_audio:
        from robot.dog.sim_audio import MockAudio
        mock_audio = MockAudio(args.mock_transcript)
    if not 0.05 <= args.speed <= 0.45:
        parser.error("--speed must be 0.05-0.45")
    sim_conn_factory = None
    if args.sim:
        from robot.dog.sim import SimDog
        sim_conn_factory = SimDog.from_cli(args.sim).connect_factory()
        for name in ("memory_file", "spacetime_file"):  # simulated sightings never land in the hardware recordings
            if getattr(args, name) == parser.get_default(name):
                setattr(args, name, getattr(args, name).replace(".data/hardware/", ".data/sim/"))
    if MOTION_INHIBIT_PATH.exists() and not args.sim:
        _say("physical motion inhibited by the active hardware task")
        return 2
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    if not args.mock_audio:
        for line in Path(".env").read_text().splitlines() if Path(".env").exists() else []:  # voice keys only
            if line.startswith(("ELEVENLABS_", "DEEPGRAM_")) and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    if args.view_host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("ANNIE_BODY_TOKEN"):
        parser.error("--view-host beyond loopback requires ANNIE_BODY_TOKEN (fail closed: anyone on the LAN could move the robot)")
    if mock_audio is not None:
        _say("voice: simulation mock (no audio devices or providers)")
    else:
        _say(f"voice: {voice().enabled} (cloud when a key is set; local say/Whisper otherwise)")
    logging.disable(logging.CRITICAL)
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    # Shell background jobs may inherit ignored SIGINT. TERM from the supervisor must
    # unwind asyncio too, so cancellation runs the neutral/StopMove/disconnect cleanup.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if previous_signals[signal.SIGINT] == signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        report = asyncio.run(run_patrol_greet(ip=args.ip, aes_key=os.environ.get("UNITREE_AES_128_KEY"),
                                              conn_factory=sim_conn_factory, source="simulation" if args.sim else "hardware", audio=mock_audio,
                                              duration_s=args.duration, speed_mps=args.speed, boundary_m=args.boundary,
                                              lidar=not args.no_lidar, firmware_avoid=args.firmware_avoid,
                                              stop_on_checkin=args.stop_on_checkin, idle_trick_s=args.idle_trick,
                                              view=LiveView(port=args.view_port, host=args.view_host), no_motion=args.no_motion, start_paused=args.start_paused, manual_control=args.manual_control,
                                              require_grandma_selection=args.require_grandma_selection,
                                              demo_everyone_grandma=args.demo_everyone_grandma,
                                              autonomous_demo=args.autonomous_demo,
                                              imgsz=args.imgsz, diag_every_s=args.diag_every,
                                              brain=VisionBrain() if args.brain else None, brain_period_s=args.brain_period,
                                              memory=SightingMemory(args.memory_file),
                                              voice=CommandListener(lambda c, t: None, status=_say,
                                                                    transcriber=lambda wav: voice().transcribe(wav) or "") if args.voice else None,
                                              identifier=(TargetIdentifier(*args.target.split(":", 1)) if args.target else None),
                                              recorder=(SpacetimeRecorder(path=args.spacetime_file) if SpacetimeRecorder else None)))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130
    finally:
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload, flush=True)
    _say(f"diag {json.dumps(report.get('diag'))}")
    _say(f"brain decisions={len(report['brain']['decisions'])} failures={report['brain']['failures']} "
         f"voice commands={len(report['voice']['commands'])}")
    _say(f"{report['reason']} greetings={len(report['greetings'])} checkins={len(report['checkins'])} "
         f"idle_tricks={len(report.get('idle_tricks', []))} collisions={len(report['collisions'])} "
         f"lidar_maps={report['lidar']['maps']} modes={report['modes']} frames={report['frames']} "
         f"moves={report['moves_sent']} stop_ack={report['stop']['ack_ms']} ms")
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0 if report["completed"] else 1)


if __name__ == "__main__":
    main()
