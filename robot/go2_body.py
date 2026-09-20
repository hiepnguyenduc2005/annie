#!/usr/bin/env python3
"""Body service for the physical Go2: the "hand and arm" side of the command + video contract.

The dog does no thinking. Whatever brain runs off-robot (the GX10, this Mac,
Claude behind the phone app) sends one succinct command at a time to this
service, watches its receipt, and looks through `GET /frame.jpg`. This process
owns the single WebRTC link to the robot and turns the fixed vocabulary below
into the sport-mode requests, patrol/follow loops, host speaker and host
microphone that already ran on this dog tonight. Commands outside the
vocabulary, out-of-range arguments, a second motion command while one is
executing, a stale link or a battery under the floor are refused with a
reason, never guessed at.

HTTP (default 0.0.0.0:8001; `X-Body-Token` must match `ANNIE_BODY_TOKEN` when set):
  GET  /status                 link, battery, pose, people currently tracked, current command
  GET  /frame.jpg              latest camera frame (404 before the first frame)
  POST /command                {"command_id"?, "name", "args"} -> 202 receipt; same id -> 200; busy -> 409
  GET  /command/{command_id}   receipt: accepted | executing | completed | failed | cancelled
  POST /stop                   cancel the executing command and send a priority StopMove

Vocabulary: stand, sit, stop, hello, stretch, heart, dance | move{vx,wz,duration_s}
| patrol{duration_s} (smart patrol with LiDAR/stall collision handling)
| find_person{name?,timeout_s,approach} | say{text} (host speaker) | listen{max_s} (host mic).

Software stop only: a lost link cannot stop the robot from here, and receipts
report firmware acknowledgments, not proof that the motion happened.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_follow import follow_command  # noqa: E402
from go2_patrol_greet import (TOPIC_LIDAR_SWITCH, TOPIC_VOXELS, _default_conn_factory, _encode)  # noqa: E402
from go2_probe import extract_lowstate, extract_pose, safe_error  # noqa: E402
from go2_smart_patrol import (PatrolPlanner, StallDetector, body_frame, quaternion_yaw, sector_ranges,  # noqa: E402
                              voxel_points_world)
from go2_walk import (MIN_OPERATING_SOC_PERCENT, MOTION_INHIBIT_PATH, TOPIC_LOWSTATE, TOPIC_POSE,  # noqa: E402
                      TOPIC_SPORT, _private_ipv4, _status_code)

SCRIPT_VERSION = "go2-body/0.1"
STAND_UP, BALANCE_STAND, STOP_MOVE, MOVE = 1004, 1002, 1003, 1008
TRICK_IDS = {"stand": 1004, "sit": 1009, "hello": 1016, "stretch": 1017, "heart": 1036, "dance": 1022}
TRICK_SETTLE_S = {"stand": 2.0, "sit": 3.0, "hello": 4.0, "stretch": 4.0, "heart": 4.0, "dance": 9.0}
MOTION = {"move", "patrol", "find_person"} | set(TRICK_IDS)
TERMINAL = {"completed", "failed", "cancelled"}
MAX_RECEIPTS = 200


def validate_command(payload) -> tuple[str, str, dict]:
    """Return (command_id, name, args) or raise ValueError with a client-facing reason."""
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    name = payload.get("name")
    args = payload.get("args", {}) or {}
    command_id = payload.get("command_id") or str(uuid.uuid4())
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 80:
        raise ValueError("command_id must be a string of 1-80 characters")
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    if name in TRICK_IDS or name == "stop":
        extra = set(args)
        if extra:
            raise ValueError(f"{name} takes no args")
        return command_id, name, {}

    def num(key, lo, hi, default=None):
        v = args.get(key, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f"{key} must be a number in [{lo}, {hi}]")
        return float(v)

    if name == "move":
        return command_id, name, {"vx": num("vx", -0.2, 0.4, 0.0), "wz": num("wz", -0.8, 0.8, 0.0),
                                  "duration_s": num("duration_s", 0.1, 10.0)}
    if name == "patrol":
        return command_id, name, {"duration_s": num("duration_s", 1.0, 300.0)}
    if name == "find_person":
        person = args.get("name")
        if person is not None and (not isinstance(person, str) or not 1 <= len(person) <= 80):
            raise ValueError("name must be a string of 1-80 characters or null")
        approach = args.get("approach", True)
        if not isinstance(approach, bool):
            raise ValueError("approach must be a boolean")
        return command_id, name, {"name": person, "timeout_s": num("timeout_s", 1.0, 120.0, 60.0), "approach": approach}
    if name == "say":
        text = args.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
            raise ValueError("text must be 1-300 characters")
        return command_id, name, {"text": text.strip()}
    if name == "listen":
        return command_id, name, {"max_s": num("max_s", 1.0, 15.0, 8.0)}
    raise ValueError(f"unknown command {name!r}")


class Refused(Exception):
    """A command the body will not run; the reason goes into the receipt."""


def _log(text):
    print(f"go2-body: {text}", file=sys.stderr, flush=True)


def _speak_blocking(text: str) -> bool:
    try:
        return subprocess.run(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode == 0
    except Exception:
        return False


def _listen_blocking(max_s: float) -> dict:
    """Host microphone -> transcript via the live listener's VAD + local Whisper (real path)."""
    from robot.simulation.live_listener import capture_utterance, pcm16_to_wav, silero_vad, sounddevice_recorder
    from robot.simulation.local_stt import LocalSTTAdapter
    utterance = capture_utterance(sounddevice_recorder(), silero_vad(), max_ms=int(max_s * 1000))
    if utterance["outcome"] != "speech":
        return {"transcript": None, "heard": False, "speech_ms": 0}
    result = LocalSTTAdapter().transcribe(pcm16_to_wav(utterance["pcm"]))
    text = (result.text or "").strip()
    return {"transcript": text or None, "heard": bool(text), "speech_ms": utterance["speech_ms"]}


class Body:
    """Owns the robot link and runs one command at a time."""

    def __init__(self, *, ip, aes_key, conn_factory=None, tracker=None, encoder=_encode, speaker=_speak_blocking,
                 listener=_listen_blocking, min_soc=MIN_OPERATING_SOC_PERCENT, stale_s=1.5, rate_hz=10.0,
                 status=_log, clock=time.time):
        self.ip, self.aes_key = ip, aes_key
        self.conn_factory = conn_factory or _default_conn_factory
        self._tracker = tracker
        self.encoder, self.speaker, self.listener = encoder, speaker, listener
        self.min_soc, self.stale_s, self.tick = min_soc, stale_s, 1.0 / rate_hz
        self.status_fn, self.clock = status, clock
        self.loop: asyncio.AbstractEventLoop | None = None
        self.conn = None
        self.link = "disconnected"
        self.tel = {"soc": None, "low_t": None, "pose": None, "pose_t": None, "yaw": 0.0, "ranges": None, "ranges_t": None}
        self.latest = {"frame": None, "jpeg": None, "seq": 0, "t": None, "w": None, "h": None}
        self.people: list[dict] = []
        self.receipts: OrderedDict[str, dict] = OrderedDict()
        self.current: dict | None = None
        self._task: asyncio.Task | None = None
        self._seq = 0
        self._closing = False

    # -- link ---------------------------------------------------------------------------------------------------
    @property
    def tracker(self):
        if self._tracker is None:
            from robot.simulation.person_tracker import PersonTracker
            face_index = None
            with contextlib.suppress(Exception):
                from robot.simulation.face_id import DEFAULT_FACES_DIR, INDEX_FILENAME, FaceIndex
                index_path = Path(DEFAULT_FACES_DIR) / INDEX_FILENAME
                if index_path.exists():
                    face_index = FaceIndex().load(index_path)
            self._tracker = PersonTracker(conf=0.35, face_index=face_index)
        return self._tracker

    def _on_low(self, msg):
        with contextlib.suppress(Exception):
            self.tel["soc"], self.tel["low_t"] = extract_lowstate(msg)["battery"]["soc_percent"], self.loop.time()

    def _on_pose(self, msg):
        with contextlib.suppress(Exception):
            pose = extract_pose(msg)
            p = pose["position"]
            self.tel["pose"], self.tel["pose_t"] = (p["x"], p["y"]), self.loop.time()
            self.tel["yaw"] = quaternion_yaw(pose["orientation_xyzw"])

    def _on_voxels(self, msg):
        with contextlib.suppress(Exception):
            data = msg.get("data") or {}
            decoded = data.get("data")
            if isinstance(decoded, dict) and self.tel["pose"] is not None:
                pts = body_frame(voxel_points_world(decoded, data), self.tel["pose"], self.tel["yaw"])
                self.tel["ranges"], self.tel["ranges_t"] = sector_ranges(pts), self.loop.time()

    async def _on_track(self, track):
        while not self._closing:
            try:
                frame = await track.recv()
            except Exception:
                return
            self.latest["frame"], self.latest["seq"], self.latest["t"] = frame, self.latest["seq"] + 1, self.loop.time()

    async def connect(self, attempts=3, retry_s=12.0) -> bool:
        self.loop = asyncio.get_running_loop()
        for attempt in range(attempts):
            self.link = "connecting"
            conn = self.conn_factory(self.ip, self.aes_key)
            task = asyncio.create_task(conn.connect())
            while getattr(conn, "video", None) is None and not task.done():
                await asyncio.sleep(0.005)
            if getattr(conn, "video", None) is not None:
                conn.video.add_track_callback(self._on_track)
            try:
                await asyncio.wait_for(task, 25)
            except Exception as exc:
                self.status_fn(f"connect attempt {attempt + 1} failed ({type(exc).__name__})")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.disconnect(), 5)
                await asyncio.sleep(retry_s)
                continue
            self.conn = conn
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.datachannel.disableTrafficSaving(True), 10)
            conn.datachannel.pub_sub.subscribe(TOPIC_LOWSTATE, self._on_low)
            conn.datachannel.pub_sub.subscribe(TOPIC_POSE, self._on_pose)
            conn.datachannel.pub_sub.subscribe(TOPIC_VOXELS, self._on_voxels)
            with contextlib.suppress(Exception):
                conn.datachannel.pub_sub.publish_without_callback(TOPIC_LIDAR_SWITCH, "on")
            with contextlib.suppress(Exception):
                conn.datachannel.switchVideoChannel(True)
            self.link = "connected"
            self.status_fn(f"connected to {self.ip}")
            return True
        self.link = "disconnected"
        return False

    async def close(self):
        self._closing = True
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        if self.conn is not None:
            with contextlib.suppress(Exception):
                await self._request(STOP_MOVE, priority=True, timeout=2.0)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.disconnect(), 5)
        self.link = "disconnected"

    def _telemetry_fresh(self) -> bool:
        now = self.loop.time()
        return (self.tel["low_t"] is not None and self.tel["pose_t"] is not None
                and now - self.tel["low_t"] <= self.stale_s and now - self.tel["pose_t"] <= self.stale_s)

    # -- robot primitives -----------------------------------------------------------------------------------------
    async def _request(self, api_id, priority=False, timeout=5.0):
        options = {"api_id": api_id}
        if priority:
            options["priority"] = 1
        resp = await asyncio.wait_for(self.conn.datachannel.pub_sub.publish_request_new(TOPIC_SPORT, options), timeout)
        return _status_code(resp)

    def _send_move(self, vx, wz):
        self._seq += 1
        self.conn.datachannel.pub_sub.publish_without_callback(
            TOPIC_SPORT, data={"header": {"identity": {"id": self._seq, "api_id": MOVE}},
                               "parameter": json.dumps({"x": float(vx), "y": 0.0, "z": float(wz)})}, msg_type="req")

    def _guard_motion(self):
        if MOTION_INHIBIT_PATH.exists():
            raise Refused("physical motion inhibited by the active hardware task")
        if self.link != "connected":
            raise Refused(f"robot link is {self.link}")
        if not self._telemetry_fresh():
            raise Refused("telemetry stale")
        if self.tel["soc"] is None or self.tel["soc"] < self.min_soc:
            raise Refused(f"battery {self.tel['soc']}% under the {self.min_soc:.0f}% floor")

    def _update_people(self):
        if self.latest["frame"] is None:
            return
        jpeg, w, h = self.encoder(self.latest["frame"])
        self.latest["jpeg"], self.latest["w"], self.latest["h"] = jpeg, w, h
        self.people = self.tracker.update(jpeg, now_ms=int(self.clock() * 1000))

    async def _drive(self, duration_s, velocity_fn, receipt):
        """Stream Move at the tick rate until velocity_fn returns done or the duration elapses."""
        start = self.loop.time()
        last_seq = -1
        result = None
        try:
            while self.loop.time() - start < duration_s:
                self._guard_motion()
                if self.latest["seq"] != last_seq:
                    last_seq = self.latest["seq"]
                    self._update_people()
                now = self.loop.time()
                ranges = self.tel["ranges"] if self.tel["ranges_t"] and now - self.tel["ranges_t"] <= 2.0 else None
                vx, wz, done, result = velocity_fn(now - start, self.people, ranges)
                receipt["progress"] = {"elapsed_s": round(now - start, 1), "vx": round(vx, 2), "wz": round(wz, 2)}
                if done:
                    break
                self._send_move(vx, wz)
                await asyncio.sleep(self.tick)
        finally:
            with contextlib.suppress(Exception):
                self._send_move(0.0, 0.0)
                receipt["stop_code"] = await self._request(STOP_MOVE, priority=True, timeout=3.0)
        return result

    # -- commands -------------------------------------------------------------------------------------------------
    async def _cmd_trick(self, name, receipt):
        self._guard_motion()
        codes = {}
        if name not in ("stand", "sit"):
            codes["stand"] = await self._request(STAND_UP, timeout=5.0)
            await asyncio.sleep(1.0)
            codes["balance"] = await self._request(BALANCE_STAND, timeout=5.0)
            await asyncio.sleep(1.0)
        try:
            codes[name] = await self._request(TRICK_IDS[name], timeout=8.0)
        except asyncio.TimeoutError:
            codes[name] = "no_ack"  # long behaviours (dance) do not ack within the window; the request was sent
        await asyncio.sleep(TRICK_SETTLE_S[name])
        return {"codes": codes, "note": "firmware acknowledgments, not evidence the motion happened"}

    async def _cmd_move(self, args, receipt):
        self._guard_motion()
        await self._request(STAND_UP, timeout=5.0)
        await asyncio.sleep(0.5)

        def const(_t, _people, _ranges):
            return args["vx"], args["wz"], False, {"vx": args["vx"], "wz": args["wz"]}
        result = await self._drive(args["duration_s"], const, receipt)
        return {**(result or {}), "duration_s": args["duration_s"]}

    async def _cmd_patrol(self, args, receipt):
        self._guard_motion()
        await self._request(STAND_UP, timeout=5.0)
        await asyncio.sleep(0.5)
        planner, stall = PatrolPlanner(), StallDetector()
        origin = self.tel["pose"]
        state = {"cmd_vx": 0.0, "collisions": 0, "modes": {}}

        def step(t, _people, ranges):
            stalled = stall.update(now_s=t, pose_xy=self.tel["pose"], commanded_vx=state["cmd_vx"])
            state["collisions"] += int(stalled)
            vx, wz, mode = planner.step(now_s=t, ranges=ranges, pose_xy=self.tel["pose"], yaw=self.tel["yaw"],
                                        origin_xy=origin, stalled=stalled)
            state["cmd_vx"] = vx
            state["modes"][mode] = state["modes"].get(mode, 0) + 1
            return vx, wz, False, {"collisions": state["collisions"], "modes": dict(state["modes"])}
        result = await self._drive(args["duration_s"], step, receipt)
        return {**(result or {}), "distance_from_origin_m": round(math.dist(origin, self.tel["pose"]), 2)}

    async def _cmd_find_person(self, args, receipt):
        self._guard_motion()
        await self._request(STAND_UP, timeout=5.0)
        await asyncio.sleep(0.5)
        planner, stall = PatrolPlanner(cruise_mps=0.2), StallDetector()
        origin = self.tel["pose"]
        wanted = (args["name"] or "").lower() or None
        state = {"cmd_vx": 0.0, "found": None, "approached": False, "hold_ticks": 0}

        def pick(people):
            upright = [p for p in people if p.get("track_id") is not None and p.get("posture") != "lying"]
            if wanted:
                named = [p for p in upright if ((p.get("identity") or {}).get("name") or "").lower() == wanted]
                if named:
                    return named[0], True
            if not upright:
                return None, False
            biggest = max(upright, key=lambda p: p["box"][3] - p["box"][1])
            height = (biggest["box"][3] - biggest["box"][1]) / float(self.latest["h"] or 480)
            return (biggest, False) if height >= 0.2 else (None, False)

        def step(t, people, ranges):
            if state["found"] is None:
                person, matched = pick(people)
                if person is None:
                    stalled = stall.update(now_s=t, pose_xy=self.tel["pose"], commanded_vx=state["cmd_vx"])
                    vx, wz, _mode = planner.step(now_s=t, ranges=ranges, pose_xy=self.tel["pose"], yaw=self.tel["yaw"],
                                                 origin_xy=origin, stalled=stalled)
                    state["cmd_vx"] = vx
                    return vx, wz, False, None
                state["found"] = {"track_id": person["track_id"], "identity": person.get("identity"), "matched": matched,
                                  "found_at_s": round(t, 1)}
                receipt["progress"] = {"found": state["found"]}
                if not args["approach"]:
                    return 0.0, 0.0, True, state["found"]
            track = next((p for p in people if p.get("track_id") == state["found"]["track_id"]), None)
            if track is None:
                state["hold_ticks"] += 1
                return 0.0, 0.0, state["hold_ticks"] > 30, state["found"]  # lost for ~3 s: stop where we are
            state["hold_ticks"] = 0
            vx, wz, reason = follow_command(track["box"], self.latest["w"] or 640, self.latest["h"] or 480, max_vx=0.3)
            if reason == "too_close" or (reason == "centered" and abs(vx) < 0.03):
                state["approached"] = True
                return 0.0, 0.0, True, state["found"]
            state["cmd_vx"] = vx
            return vx, wz, False, state["found"]
        found = await self._drive(args["timeout_s"], step, receipt)
        return {"found": found is not None, "track_id": (found or {}).get("track_id"),
                "identity": (found or {}).get("identity"), "matched_name": bool((found or {}).get("matched")),
                "approached": state["approached"], "searched_s": receipt.get("progress", {}).get("elapsed_s")}

    async def _cmd_say(self, args, receipt):
        played = await self.loop.run_in_executor(None, self.speaker, args["text"])
        return {"played": bool(played), "where": "host speaker"}

    async def _cmd_listen(self, args, receipt):
        result = await self.loop.run_in_executor(None, self.listener, args["max_s"])
        return {**result, "where": "host microphone"}

    async def _execute(self, receipt):
        name, args = receipt["name"], receipt["args"]
        receipt["state"], receipt["started_at_ms"] = "executing", int(self.clock() * 1000)
        self.status_fn(f"{name} {args if args else ''} executing")
        try:
            if name in TRICK_IDS:
                result = await self._cmd_trick(name, receipt)
            else:
                result = await getattr(self, f"_cmd_{name}")(args, receipt)
            receipt["state"], receipt["result"] = "completed", result
        except asyncio.CancelledError:
            receipt["state"], receipt["error"] = "cancelled", "stopped by operator"
            raise
        except Refused as exc:
            receipt["state"], receipt["error"] = "failed", str(exc)
        except Exception as exc:
            receipt["state"], receipt["error"] = "failed", safe_error(exc)
        finally:
            receipt["finished_at_ms"] = int(self.clock() * 1000)
            if self.current is receipt:
                self.current = None
            self.status_fn(f"{name} {receipt['state']}" + (f": {receipt['error']}" if receipt.get("error") else ""))

    def _remember(self, receipt):
        self.receipts[receipt["command_id"]] = receipt
        while len(self.receipts) > MAX_RECEIPTS:
            self.receipts.popitem(last=False)

    async def submit(self, payload) -> tuple[int, dict]:
        """Accept a command; returns (http_status, receipt-or-error)."""
        try:
            command_id, name, args = validate_command(payload)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        if command_id in self.receipts:
            return 200, self.receipts[command_id]
        if name == "stop":
            receipt = {"command_id": command_id, "name": name, "args": {}, "state": "executing",
                       "accepted_at_ms": int(self.clock() * 1000), "result": None, "error": None}
            self._remember(receipt)
            receipt["result"] = await self.stop()
            receipt["state"], receipt["finished_at_ms"] = "completed", int(self.clock() * 1000)
            return 200, receipt
        if self.current is not None and self.current["state"] in ("accepted", "executing"):
            return 409, {"error": "busy", "current": self.current}
        receipt = {"command_id": command_id, "name": name, "args": args, "state": "accepted",
                   "accepted_at_ms": int(self.clock() * 1000), "result": None, "error": None}
        self._remember(receipt)
        self.current = receipt
        self._task = asyncio.create_task(self._execute(receipt))
        return 202, receipt

    async def stop(self) -> dict:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        code = None
        t0 = self.loop.time()
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self._send_move(0.0, 0.0)
                code = await self._request(STOP_MOVE, priority=True, timeout=3.0)
        return {"stop_code": code, "ack_ms": round((self.loop.time() - t0) * 1000, 1),
                "note": "software stop, not a hardware emergency stop"}

    def status(self) -> dict:
        now = self.loop.time() if self.loop else None
        pose = self.tel["pose"]
        ranges = self.tel["ranges"]
        return {"script": SCRIPT_VERSION, "link": self.link, "battery_soc_percent": self.tel["soc"],
                "telemetry_fresh": bool(self.loop and self._telemetry_fresh()),
                "pose": None if pose is None else {"x": round(pose[0], 3), "y": round(pose[1], 3), "yaw": round(self.tel["yaw"], 3)},
                "ranges_m": None if not ranges else {k: (None if v == float("inf") else round(v, 2))
                                                     for k, v in ranges.items() if k in ("front", "left", "right")},
                "frame_age_ms": None if self.latest["t"] is None or now is None else int((now - self.latest["t"]) * 1000),
                "people": self.people, "command": self.current}

    def frame(self) -> bytes | None:
        if self.latest["frame"] is None:
            return None
        if self.latest["jpeg"] is None or self.latest["seq"] != self.latest.get("jpeg_seq"):
            jpeg, w, h = self.encoder(self.latest["frame"])
            self.latest["jpeg"], self.latest["w"], self.latest["h"], self.latest["jpeg_seq"] = jpeg, w, h, self.latest["seq"]
        return self.latest["jpeg"]


# -- HTTP (stdlib server in a thread, coroutines run on the body's loop) -----------------------------------------------

def make_handler(body: Body, loop: asyncio.AbstractEventLoop, token: str | None):
    def run(coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload, content_type="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if token and self.headers.get("X-Body-Token") != token:
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == "/status":
                return self._send(200, body.status())
            if self.path == "/frame.jpg":
                jpeg = body.frame()
                return self._send(200, jpeg, "image/jpeg") if jpeg else self._send(404, {"error": "no frame yet"})
            if self.path.startswith("/command/"):
                receipt = body.receipts.get(self.path[len("/command/"):])
                return self._send(200, receipt) if receipt else self._send(404, {"error": "unknown command_id"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                return self._send(413, {"error": "body too large"})
            raw = self.rfile.read(length) if length else b""
            if self.path == "/stop":
                return self._send(200, run(body.stop()))
            if self.path == "/command":
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                code, result = run(body.submit(payload))
                return self._send(code, result)
            self._send(404, {"error": "not found"})

    return Handler


def serve(body: Body, host: str, port: int, token: str | None):
    loop = asyncio.get_running_loop()
    server = ThreadingHTTPServer((host, port), make_handler(body, loop, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def run_service(*, ip, aes_key, host, port, token, ready=None):
    body = Body(ip=ip, aes_key=aes_key)
    server = serve(body, host, port, token)
    _log(f"listening on http://{host}:{port} (token {'set' if token else 'not set'})")
    if ready is not None:
        ready.set()
    try:
        while not body._closing:
            if body.link != "connected" or (body.loop and body.tel["low_t"] is not None and not body._telemetry_fresh()
                                            and body.loop.time() - body.tel["low_t"] > 10.0):
                if body.conn is not None:
                    _log("link stale; reconnecting")
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(body.conn.disconnect(), 5)
                    body.conn, body.link = None, "disconnected"
                await body.connect()
            await asyncio.sleep(1.0)
    finally:
        server.shutdown()
        await body.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Command + video body service for the physical Go2.")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run_service(ip=args.ip, aes_key=os.environ.get("UNITREE_AES_128_KEY"), host=args.host,
                                port=args.port, token=os.environ.get("ANNIE_BODY_TOKEN") or None))
    except KeyboardInterrupt:
        _log("interrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
