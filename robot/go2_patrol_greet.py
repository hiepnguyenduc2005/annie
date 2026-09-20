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
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_probe import extract_lowstate, extract_pose, safe_error  # noqa: E402
from go2_smart_patrol import (PatrolPlanner, StallDetector, body_frame, quaternion_yaw, sector_ranges,  # noqa: E402
                              voxel_points_world)
from go2_walk import (MIN_OPERATING_SOC_PERCENT, MOTION_INHIBIT_PATH, TOPIC_LOWSTATE, TOPIC_POSE,  # noqa: E402
                      TOPIC_SPORT, _private_ipv4, _status_code)

SCRIPT_VERSION = "go2-patrol-greet/0.2"
STAND_UP, BALANCE_STAND, STOP_MOVE, MOVE, HELLO = 1004, 1002, 1003, 1008, 1016
TOPIC_VOXELS, TOPIC_LIDAR_SWITCH, TOPIC_SPORT_STATE = "rt/utlidar/voxel_map_compressed", "rt/utlidar/switch", "rt/lf/sportmodestate"
TOPIC_AVOID, AVOID_SWITCH_SET, AVOID_USE_API = "rt/api/obstacles_avoid/request", 1001, 1004


class GreetPolicy:
    """Deterministic greeting decisions from tracker output. No hardware, no model."""

    def __init__(self, *, cooldown_s: float = 30.0, min_height_frac: float = 0.25):
        self.cooldown_s = cooldown_s
        self.min_height_frac = min_height_frac
        self.greeted: dict[int, float] = {}

    def step(self, tracks, frame_w, frame_h, *, now_s):
        """Return ('patrol', None) | ('greet', track_id) | ('checkin', track_id)."""
        lying = [t for t in tracks if t.get("posture") == "lying" and t.get("lying_frames", 0) >= 2]
        if lying:
            return "checkin", lying[0].get("track_id")
        candidates = []
        for t in tracks:
            tid = t.get("track_id")
            if tid is None:
                continue
            height = (t["box"][3] - t["box"][1]) / float(frame_h)
            if height < self.min_height_frac:
                continue
            last = self.greeted.get(tid)
            if last is not None and now_s - last < self.cooldown_s:
                continue
            candidates.append((height, tid))
        if not candidates:
            return "patrol", None
        _, tid = max(candidates)
        self.greeted[tid] = now_s
        return "greet", tid

    @staticmethod
    def greeting_text(track) -> str:
        identity = track.get("identity") or {}
        name = identity.get("name")
        return f"Hi {name}!" if name else "Hello there!"


def _default_conn_factory(ip, aes_key):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key)


def _say(text):
    print(f"go2-patrol-greet: {text}", file=sys.stderr, flush=True)


def _speak_host(text: str):
    """Speak on the host speaker (macOS say); the dog itself has no verified speaker."""
    with contextlib.suppress(Exception):
        subprocess.Popen(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _encode(frame):
    import cv2
    img = frame.to_ndarray(format="bgr24")
    h, w = img.shape[:2]
    if w > 640:
        img = cv2.resize(img, (640, int(h * 640 / w)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes(), img.shape[1], img.shape[0]


async def run_patrol_greet(*, ip, aes_key, duration_s=300.0, speed_mps=0.25, yaw_rps=0.5, boundary_m=2.5,
                           rate_hz=10.0, stale_s=1.0, min_soc=MIN_OPERATING_SOC_PERCENT, conn_factory=None,
                           tracker=None, encoder=_encode, speak=_speak_host, policy=None, status=_say,
                           planner=None, stall=None, lidar=True, firmware_avoid=False, lidar_stale_s=2.0):
    loop = asyncio.get_running_loop()
    report = {"script": SCRIPT_VERSION, "source": "hardware", "target_ip": ip, "connection": {"status": "not_started"},
              "battery_soc_start": None, "frames": 0, "moves_sent": 0, "greetings": [], "checkins": [],
              "collisions": [], "modes": {}, "lidar": {"maps": 0, "first_ranges": None, "stale_ticks": 0},
              "firmware_avoid": None, "elapsed_s": 0.0, "max_distance_from_origin_m": 0.0, "reason": None,
              "completed": False, "stop": {"requested": False, "ack_ms": None, "code": None},
              "notes": ["Software StopMove only; host cannot stop the robot over a lost link.",
                        "Speech is played on the host, not the robot.",
                        "Collision detection = LiDAR voxel sectors + odometry stall; no touch sensors on this robot."]}
    policy = policy or GreetPolicy()
    planner = planner or PatrolPlanner(cruise_mps=speed_mps, turn_rps=yaw_rps, leash_m=boundary_m * 0.8)
    stall = stall or StallDetector()
    if tracker is None:
        from robot.simulation.person_tracker import PersonTracker
        tracker = PersonTracker(conf=0.35)
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
            tel["pose"], tel["pose_t"] = (p["x"], p["y"]), loop.time()
            tel["yaw"] = quaternion_yaw(pose["orientation_xyzw"])
        except Exception:
            pass

    def on_voxels(msg):
        # The driver already decoded the voxel payload; reduce it to front/left/right ranges in the body frame.
        try:
            data = msg.get("data") or {}
            decoded = data.get("data")
            if not isinstance(decoded, dict) or tel["pose"] is None:
                return
            pts = body_frame(voxel_points_world(decoded, data), tel["pose"], tel["yaw"])
            tel["ranges"], tel["ranges_t"] = sector_ranges(pts), loop.time()
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

    async def on_track(track):
        while not stopped:
            try:
                frame = await track.recv()
            except Exception:
                return
            latest["frame"], latest["seq"] = frame, latest["seq"] + 1

    async def request(api_id, priority=False, timeout=5.0):
        options = {"api_id": api_id}
        if priority:
            options["priority"] = 1
        resp = await asyncio.wait_for(conn.datachannel.pub_sub.publish_request_new(TOPIC_SPORT, options), timeout)
        return _status_code(resp)

    def send_move(vx, wz):
        nonlocal seq
        seq += 1
        conn.datachannel.pub_sub.publish_without_callback(
            TOPIC_SPORT, data={"header": {"identity": {"id": seq, "api_id": MOVE}},
                               "parameter": json.dumps({"x": float(vx), "y": 0.0, "z": float(wz)})}, msg_type="req")
        report["moves_sent"] += 1

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
        origin = tel["pose"]
        lidar_state = "waiting" if lidar else "off"
        if lidar:
            deadline = loop.time() + 4
            while loop.time() < deadline and tel["ranges"] is None:
                await asyncio.sleep(0.05)
            lidar_state = f"live {report['lidar']['first_ranges']}" if tel["ranges"] is not None else "no voxel maps yet"
        status(f"connected, battery {tel['soc']:.0f}%, camera live, lidar {lidar_state}, "
               f"firmware_avoid={report['firmware_avoid']}, range_obstacle={tel['range_obstacle']}, "
               f"origin {origin[0]:.2f},{origin[1]:.2f}; patrolling")
        commanded = True
        for api in (STAND_UP, BALANCE_STAND):
            code = await request(api)
            if code not in (0, None):
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
        while loop.time() - start < duration_s:
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
            if latest["seq"] != last_seq:
                last_seq = latest["seq"]
                jpeg, fw, fh = encoder(latest["frame"])
                tracks = tracker.update(jpeg, now_ms=int(time.time() * 1000))
                report["frames"] += 1
            action, tid = policy.step(tracks, fw or 640, fh or 480, now_s=now) if fw else ("patrol", None)
            if action == "greet":
                track = next(t for t in tracks if t.get("track_id") == tid)
                text = policy.greeting_text(track)
                send_move(0.0, 0.0)
                status(f"t={now-start:5.1f}s person (track {tid}) ahead: stopping, Hello, saying {text!r}")
                code = await request(STOP_MOVE, priority=True, timeout=3.0)
                speak(text)
                hello_code = await request(HELLO, timeout=8.0)
                report["greetings"].append({"t_s": round(now - start, 1), "track_id": tid, "text": text,
                                            "hello_code": hello_code, "identity": track.get("identity")})
                await asyncio.sleep(4.0)  # let Hello finish before walking again
                with contextlib.suppress(Exception):
                    await request(BALANCE_STAND, timeout=3.0)
                await asyncio.sleep(1.0)
                cmd_vx = 0.0
                stall.update(now_s=loop.time(), pose_xy=tel["pose"], commanded_vx=0.0)
                continue
            if action == "checkin":
                send_move(0.0, 0.0)
                status(f"t={now-start:5.1f}s person (track {tid}) appears to be lying down: stopping and asking")
                await request(STOP_MOVE, priority=True, timeout=3.0)
                speak("Are you okay? Please say okay or help.")
                report["checkins"].append({"t_s": round(now - start, 1), "track_id": tid})
                report["reason"] = "checkin_raised"
                return report  # hand over to the incident flow; do not keep patrolling
            ranges = tel["ranges"]
            if lidar and (tel["ranges_t"] is None or now - tel["ranges_t"] > lidar_stale_s):
                ranges = None  # unknown: the planner treats missing sectors as clear, stall detection still covers us
                report["lidar"]["stale_ticks"] += 1
            stalled = stall.update(now_s=now, pose_xy=tel["pose"], commanded_vx=cmd_vx)
            if stalled:
                report["collisions"].append({"t_s": round(now - start, 1), "mode": mode,
                                             "front_m": None if not ranges else round(min(ranges["front"], 99.0), 2)})
                front = "?" if not ranges else f"{min(ranges['front'], 99.0):.2f}"
                status(f"t={now-start:5.1f}s COLLISION: no progress while driving (front={front} m); "
                       "backing off and turning")
            cmd_vx, cmd_wz, new_mode = planner.step(now_s=now, ranges=ranges, pose_xy=tel["pose"], yaw=tel["yaw"],
                                                    origin_xy=origin, stalled=stalled)
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
        report["reason"] = "duration_complete"
    except asyncio.CancelledError:
        report["reason"] = "operator_cancelled"
    except Exception as exc:
        report["reason"] = f"error:{type(exc).__name__}"
        report["error"] = safe_error(exc)
    finally:
        stopped = True
        if conn is not None and report["connection"]["status"] == "connected":
            if commanded:
                report["stop"]["requested"] = True
                t0 = loop.time()
                with contextlib.suppress(Exception):
                    report["stop"]["code"] = await request(STOP_MOVE, priority=True, timeout=2.0)
                    report["stop"]["ack_ms"] = round((loop.time() - t0) * 1000, 1)
            with contextlib.suppress(Exception):
                conn.datachannel.switchVideoChannel(False)
            try:
                await asyncio.wait_for(conn.disconnect(), 5)
                report["connection"]["disconnected"] = True
            except Exception as exc:
                report["connection"]["disconnected"] = False
        report["completed"] = report["reason"] in ("duration_complete", "operator_cancelled", "checkin_raised") \
            and report["connection"].get("disconnected", False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Patrol a slow circle and greet people (supervised).")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--speed", type=float, default=0.25, help="m/s, at most 0.4")
    parser.add_argument("--boundary", type=float, default=2.5)
    parser.add_argument("--no-lidar", action="store_true", help="skip the voxel-map sector ranges (stall detection only)")
    parser.add_argument("--firmware-avoid", action="store_true", help="also switch on the firmware obstacle-avoid service")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not 0.05 <= args.speed <= 0.4:
        parser.error("--speed must be 0.05-0.4")
    if MOTION_INHIBIT_PATH.exists():
        _say("physical motion inhibited by the active hardware task")
        return 2
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_patrol_greet(ip=args.ip, aes_key=os.environ.get("UNITREE_AES_128_KEY"),
                                              duration_s=args.duration, speed_mps=args.speed, boundary_m=args.boundary,
                                              lidar=not args.no_lidar, firmware_avoid=args.firmware_avoid))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload, flush=True)
    _say(f"{report['reason']} greetings={len(report['greetings'])} collisions={len(report['collisions'])} "
         f"lidar_maps={report['lidar']['maps']} modes={report['modes']} frames={report['frames']} "
         f"moves={report['moves_sent']} stop_ack={report['stop']['ack_ms']} ms")
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0 if report["completed"] else 1)


if __name__ == "__main__":
    main()
