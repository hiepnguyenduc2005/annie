#!/usr/bin/env python3
"""Supervised Go2 walk: straight line or circle at low speed, with watchdogs.

Commissioning tool for HW-04/HW-05. Not a patrol planner and not a hardware
emergency stop. It sends sport-mode requests over the same WebRTC connection
as go2_probe.py and stops the robot on: duration complete, stale telemetry,
low battery, leaving the recorded boundary, any error, or Ctrl-C.
The host-side stop cannot reach the robot over a lost link; keep an operator
next to the dog.

Run from the repository root:
  .cache/dimos/.venv/bin/python robot/go2_walk.py --distance 1          # 1 m forward
  .cache/dimos/.venv/bin/python robot/go2_walk.py --circle-radius 1 --laps 3
  .cache/dimos/.venv/bin/python robot/go2_walk.py --circle-radius 1 --duration 1800
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from go2_probe import extract_lowstate, extract_pose, safe_error  # noqa: E402

SCRIPT_VERSION = "go2-walk/0.1"
TOPIC_SPORT = "rt/api/sport/request"
TOPIC_LOWSTATE = "rt/lf/lowstate"
TOPIC_POSE = "rt/utlidar/robot_pose"
# Sport api ids are identical in normal and mcf mode for these commands.
SPORT_CMD = {"BalanceStand": 1002, "StopMove": 1003, "StandUp": 1004, "Move": 1008}
MAX_SPEED_MPS = 0.6  # hard cap for commissioning runs
DEFAULT_SPEED_MPS = 0.3
DEFAULT_BOUNDARY_M = 2.5  # operator's requested 5 m patrol width
STOPPED_SPEED_MPS = 0.05


@dataclasses.dataclass(frozen=True)
class MotionPlan:
    shape: str
    vx_mps: float
    yaw_rps: float
    duration_s: float
    boundary_radius_m: float
    circle_radius_m: float | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def plan_motion(*, speed_mps: float, distance_m: float | None = None,
                circle_radius_m: float | None = None, laps: float = 1.0,
                duration_s: float | None = None,
                boundary_radius_m: float = DEFAULT_BOUNDARY_M) -> MotionPlan:
    if not (0.0 < speed_mps <= MAX_SPEED_MPS):
        raise ValueError(f"speed must be within (0, {MAX_SPEED_MPS}] m/s")
    if boundary_radius_m <= 0:
        raise ValueError("boundary radius must be positive")
    if duration_s is not None and duration_s <= 0:
        raise ValueError("duration must be positive")
    if (distance_m is None) == (circle_radius_m is None):
        raise ValueError("choose exactly one of distance (line) or circle radius")
    if circle_radius_m is not None:
        if circle_radius_m <= 0 or laps <= 0:
            raise ValueError("circle radius and laps must be positive")
        # The robot starts on the circle; the far side is 2r from the origin.
        if 2.0 * circle_radius_m > boundary_radius_m:
            raise ValueError("circle diameter exceeds the boundary radius; lower --circle-radius or raise --boundary")
        total = duration_s if duration_s is not None else laps * 2.0 * math.pi * circle_radius_m / speed_mps
        return MotionPlan("circle", speed_mps, speed_mps / circle_radius_m, total,
                          boundary_radius_m, circle_radius_m)
    if distance_m <= 0:
        raise ValueError("distance must be positive")
    total = duration_s if duration_s is not None else distance_m / speed_mps
    if speed_mps * total > boundary_radius_m + 1e-9:
        raise ValueError("a straight line that long leaves the boundary; shorten it or raise --boundary")
    return MotionPlan("line", speed_mps, 0.0, total, boundary_radius_m)


def _status_code(response) -> int | None:
    try:
        code = response["data"]["header"]["status"]["code"]
    except (KeyError, TypeError):
        return None
    return code if isinstance(code, int) else None


class _Telemetry:
    def __init__(self, clock):
        self.clock = clock
        self.soc = None
        self.low_t = None
        self.pose = None       # (x, y)
        self.pose_t = None
        self.prev_pose = None
        self.prev_t = None
        self.speed = None
        self.parse_errors = set()

    def on_lowstate(self, msg):
        try:
            self.soc = extract_lowstate(msg)["battery"]["soc_percent"]
            self.low_t = self.clock()
        except Exception as exc:
            self.parse_errors.add(f"lowstate: {type(exc).__name__}")

    def on_pose(self, msg):
        try:
            p = extract_pose(msg)["position"]
        except Exception as exc:
            self.parse_errors.add(f"pose: {type(exc).__name__}")
            return
        now = self.clock()
        if self.pose is not None and now - self.pose_t > 0.02:
            self.speed = math.dist(self.pose, (p["x"], p["y"])) / (now - self.pose_t)
        self.prev_pose, self.prev_t = self.pose, self.pose_t
        self.pose, self.pose_t = (p["x"], p["y"]), now


def _safety_reason(tel: _Telemetry, now: float, origin, plan: MotionPlan,
                   stale_s: float, min_soc: float) -> str | None:
    if tel.low_t is None or tel.pose_t is None:
        return "telemetry_missing"
    if now - tel.low_t > stale_s or now - tel.pose_t > stale_s:
        return "telemetry_stale"
    if tel.soc < min_soc:
        return "battery_low"
    if math.dist(origin, tel.pose) > plan.boundary_radius_m:
        return "boundary_exceeded"
    return None


def _default_conn_factory(ip: str, aes_key: str | None):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key)


def _say(text: str) -> None:
    print(f"go2-walk: {text}", file=sys.stderr, flush=True)


async def run_walk(plan: MotionPlan, *, ip: str, aes_key: str | None, conn_factory=None,
                   tick_s: float = 0.1, stale_s: float = 1.0, settle_s: float = 3.0,
                   first_telemetry_s: float = 5.0, min_soc: float = 20.0,
                   connect_timeout_s: float = 20.0, stop_ack_timeout_s: float = 2.0,
                   stop_observe_s: float = 3.0, status=_say) -> dict:
    loop = asyncio.get_running_loop()
    tel = _Telemetry(loop.time)
    report = {
        "script": SCRIPT_VERSION, "source": "hardware", "target_ip": ip,
        "plan": plan.as_dict(), "connection": {"status": "not_started"},
        "battery_soc_start": None, "origin": None, "final_pose": None,
        "moves_sent": 0, "elapsed_s": 0.0, "max_distance_from_origin_m": 0.0,
        "stop": {"requested": False, "ack_ms": None, "code": None,
                 "overrun_m": None, "settle_s": None},
        "reason": None, "completed": False,
        "notes": ["Host wall-clock timing; not a calibrated stopping-distance measurement.",
                  "Host-side stop cannot reach the robot over a lost link."],
    }
    conn = None
    origin = None
    motion_started = False
    seq = 0

    async def request(name: str, priority: bool = False, timeout: float = 5.0):
        options = {"api_id": SPORT_CMD[name]}
        if priority:
            options["priority"] = 1
        resp = await asyncio.wait_for(
            conn.datachannel.pub_sub.publish_request_new(TOPIC_SPORT, options), timeout)
        return _status_code(resp)

    def send_move():
        nonlocal seq
        seq += 1
        conn.datachannel.pub_sub.publish_without_callback(
            TOPIC_SPORT,
            data={"header": {"identity": {"id": seq, "api_id": SPORT_CMD["Move"]}},
                  "parameter": json.dumps({"x": plan.vx_mps, "y": 0.0, "z": plan.yaw_rps})},
            msg_type="req")
        report["moves_sent"] += 1

    async def stop_robot():
        report["stop"]["requested"] = True
        t0 = loop.time()
        try:
            code = await request("StopMove", priority=True, timeout=stop_ack_timeout_s)
            report["stop"]["ack_ms"] = round((loop.time() - t0) * 1000.0, 1)
            report["stop"]["code"] = code
        except Exception as exc:
            report["stop"]["error"] = safe_error(exc)
            return
        # Observe how far the robot keeps moving after the acknowledged stop.
        at_ack = tel.pose
        deadline = loop.time() + stop_observe_s
        quiet = 0
        while loop.time() < deadline:
            await asyncio.sleep(tick_s)
            if tel.speed is not None and tel.speed < STOPPED_SPEED_MPS:
                quiet += 1
                if quiet >= 3:
                    report["stop"]["settle_s"] = round(loop.time() - t0, 3)
                    break
            else:
                quiet = 0
        if at_ack is not None and tel.pose is not None:
            report["stop"]["overrun_m"] = round(math.dist(at_ack, tel.pose), 3)

    try:
        conn = conn_factory(ip, aes_key) if conn_factory else _default_conn_factory(ip, aes_key)
        try:
            await asyncio.wait_for(conn.connect(), connect_timeout_s)
        except Exception as exc:
            report["connection"]["status"] = "connect_failed"
            report["connection"]["error"] = safe_error(exc)
            report["reason"] = f"connect_failed:{type(exc).__name__}"
            return report
        report["connection"]["status"] = "connected"
        await asyncio.wait_for(conn.datachannel.disableTrafficSaving(True), connect_timeout_s)
        conn.datachannel.pub_sub.subscribe(TOPIC_LOWSTATE, tel.on_lowstate)
        conn.datachannel.pub_sub.subscribe(TOPIC_POSE, tel.on_pose)

        deadline = loop.time() + first_telemetry_s
        while loop.time() < deadline and (tel.low_t is None or tel.pose_t is None):
            await asyncio.sleep(tick_s)
        if tel.low_t is None or tel.pose_t is None:
            report["reason"] = "telemetry_missing"
            return report
        report["battery_soc_start"] = tel.soc
        if tel.soc < min_soc:
            report["reason"] = "battery_low"
            return report
        origin = tel.pose
        report["origin"] = {"x": origin[0], "y": origin[1]}
        status(f"connected, battery {tel.soc:.0f}%, origin x={origin[0]:.2f} y={origin[1]:.2f}")

        motion_started = True  # stand commands change posture; always stop afterwards
        for name in ("StandUp", "BalanceStand"):
            code = await request(name)
            if code not in (0, None):
                report["reason"] = f"stand_failed:{code}"
                return report
            status(f"{name} acknowledged (code {code})")
            await asyncio.sleep(settle_s)

        status(f"walking {plan.shape}: {plan.vx_mps} m/s, yaw {plan.yaw_rps:.2f} rad/s, "
               f"{plan.duration_s:.1f} s, boundary {plan.boundary_radius_m} m")
        start = loop.time()
        last_print = start
        while True:
            now = loop.time()
            report["elapsed_s"] = round(now - start, 2)
            reason = _safety_reason(tel, now, origin, plan, stale_s, min_soc)
            if reason:
                report["reason"] = reason
                return report
            dist = math.dist(origin, tel.pose)
            report["max_distance_from_origin_m"] = max(report["max_distance_from_origin_m"], round(dist, 3))
            if now - start >= plan.duration_s:
                report["reason"] = "duration_complete"
                return report
            send_move()
            if now - last_print >= 1.0:
                last_print = now
                spd = f"{tel.speed:.2f}" if tel.speed is not None else "?"
                status(f"t={now - start:5.1f}s dist_from_origin={dist:.2f} m speed={spd} m/s battery={tel.soc:.0f}%")
            await asyncio.sleep(tick_s)
    except asyncio.CancelledError:
        report["reason"] = "operator_cancelled"
    except Exception as exc:
        report["reason"] = f"error:{type(exc).__name__}"
        report["error"] = safe_error(exc)
    finally:
        if conn is not None and report["connection"]["status"] == "connected":
            if motion_started:
                status("sending StopMove")
                with contextlib.suppress(Exception):
                    await stop_robot()
            if tel.pose is not None:
                report["final_pose"] = {"x": tel.pose[0], "y": tel.pose[1]}
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=5.0)
                report["connection"]["disconnected"] = True
            except Exception as exc:
                report["connection"]["disconnected"] = False
                report["connection"]["disconnect_error"] = safe_error(exc)
        for pe in sorted(tel.parse_errors):
            report["notes"].append("parse_error: " + pe)
        stop = report["stop"]
        report["completed"] = (
            report["reason"] == "duration_complete"
            and stop["ack_ms"] is not None and stop["code"] in (0, None)
            and report["connection"].get("disconnected", False)
        )
    return report


def _private_ipv4(value: str) -> str:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a private IPv4 address literal")
    ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if parsed.version != 4 or not any(parsed in ipaddress.ip_network(n) for n in ranges):
        raise argparse.ArgumentTypeError("must be a private IPv4 address literal")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supervised low-speed Go2 walk (line or circle).")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED_MPS, help=f"m/s, max {MAX_SPEED_MPS}")
    shape = parser.add_mutually_exclusive_group(required=True)
    shape.add_argument("--distance", type=float, help="straight line, metres")
    shape.add_argument("--circle-radius", type=float, help="circle radius, metres")
    parser.add_argument("--laps", type=float, default=1.0, help="circle laps (ignored with --duration)")
    parser.add_argument("--duration", type=float, help="keep walking this many seconds")
    parser.add_argument("--boundary", type=float, default=DEFAULT_BOUNDARY_M,
                        help="stop if the robot gets this far from its start, metres")
    parser.add_argument("--min-battery", type=float, default=20.0, help="percent")
    parser.add_argument("--tick", type=float, default=0.1, help="Move resend period, seconds")
    parser.add_argument("--output", help="write the JSON report here")
    args = parser.parse_args(argv)
    try:
        plan = plan_motion(speed_mps=args.speed, distance_m=args.distance,
                           circle_radius_m=args.circle_radius, laps=args.laps,
                           duration_s=args.duration, boundary_radius_m=args.boundary)
    except ValueError as exc:
        parser.error(str(exc))
    if not (0.02 <= args.tick <= 0.5):
        parser.error("--tick must be 0.02-0.5 s")

    # Robot signaling must stay on the local link; vendor logs can echo auth material.
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    logging.disable(logging.CRITICAL)
    aes_key = os.environ.get("UNITREE_AES_128_KEY")

    try:
        report = asyncio.run(run_walk(plan, ip=args.ip, aes_key=aes_key,
                                      min_soc=args.min_battery, tick_s=args.tick))
    except KeyboardInterrupt:
        _say("interrupted before the run could report; check the robot")
        return 130
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    _say(f"{report['reason']} (completed={report['completed']}, moves={report['moves_sent']}, "
         f"max_dist={report['max_distance_from_origin_m']} m, stop_ack={report['stop']['ack_ms']} ms)")
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
