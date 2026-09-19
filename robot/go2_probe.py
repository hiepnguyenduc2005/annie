#!/usr/bin/env python3
"""Stationary read-only Go2 probe: one video frame + telemetry, no motion.

Imports unitree_webrtc_connect directly (LocalSTA, no cloud, no DimOS).
Wire topics are stable constants (see installed constants.py: RTC_TOPIC
"LOW_STATE" = rt/lf/lowstate, "ROBOTODOM" = rt/utlidar/robot_pose).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

PROBE_VERSION = "go2-probe/0.1"
TOPICS = {"lowstate": "rt/lf/lowstate", "pose": "rt/utlidar/robot_pose"}
ERROR_HINTS = {
    "AesKeyRequiredError": "This firmware requires UNITREE_AES_128_KEY from the device owner.",
    "AesKeyRejectedError": "The device did not accept the configured key.",
    "RobotBusyError": "Another client may hold the robot connection.",
    "ModuleNotFoundError": "Install robot/requirements.txt in this Python environment.",
    "LocalSignalingPortError": "The robot signaling port was unreachable.",
    "NoSdpAnswerError": "The robot returned no WebRTC answer.",
    "DataChannelTimeoutError": "The WebRTC data channel did not become ready.",
}


def safe_error(exc: Exception) -> dict:
    # Do not redact arbitrary vendor text: omit it entirely, including rejected keys.
    name = type(exc).__name__
    return {"type": name, "hint": ERROR_HINTS.get(name, "Probe failed; vendor details withheld.")}


def new_report(ip: str) -> dict:
    return {
        "probe": PROBE_VERSION,
        "source": "hardware",
        "target_ip": ip,
        "connection": {"status": "not_started"},
        "streams": {
            "video": {"received": False, "frame": None},
            "lowstate": {"received": False, "battery": None, "imu_rpy_rad": None},
            "pose": {"received": False, "position": None, "orientation_xyzw": None},
        },
        "notes": [
            "No motion commands sent; subscriptions and video only.",
            "Host wall-clock receipt timestamps; not synchronized to robot or across streams.",
        ],
        "completed": False,
    }


def _finite(value) -> float:
    if type(value) not in (int, float):
        raise ValueError("expected a numeric telemetry value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite telemetry")
    return number


def extract_lowstate(msg: dict) -> dict:
    data = msg.get("data") or {}
    bms = data.get("bms_state") or {}
    imu = data.get("imu_state") or {}
    soc = _finite(bms.get("soc"))
    if not 0.0 <= soc <= 100.0:
        raise ValueError("battery soc out of range")
    rpy = imu.get("rpy")
    if not isinstance(rpy, list) or len(rpy) != 3:
        raise ValueError("expected three IMU angles")
    return {
        "battery": {
            "soc_percent": soc,
            "power_v": _finite(data.get("power_v")),
            "current_ma": _finite(bms.get("current")),
        },
        "imu_rpy_rad": [_finite(x) for x in rpy],
    }


def extract_pose(msg: dict) -> dict:
    data = msg.get("data") or {}
    pose = data.get("pose") or {}
    position = pose.get("position") or {}
    orientation = pose.get("orientation") or {}
    header = data.get("header") or {}
    frame_id = header.get("frame_id")
    if not isinstance(frame_id, str) or not 1 <= len(frame_id) <= 100:
        raise ValueError("invalid coordinate frame")
    quaternion = {axis: _finite(orientation.get(axis)) for axis in "xyzw"}
    if not 0.9 <= sum(v * v for v in quaternion.values()) <= 1.1:
        raise ValueError("invalid rotation quaternion")
    return {
        "frame_id": frame_id,
        "position": {
            "x": _finite(position.get("x")),
            "y": _finite(position.get("y")),
            "z": _finite(position.get("z")),
        },
        "orientation_xyzw": quaternion,
    }


def _default_conn_factory(ip: str, aes_key: str | None):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection

    # username/password omitted: no cloud token fetch (webrtc_driver.py:55-59).
    return UnitreeWebRTCConnection(
        WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key
    )


async def run_probe(ip, timeout, aes_key=None, conn_factory=None, poll_interval=0.05):
    report = new_report(ip)
    conn = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    state = {"frame": False, "lowstate": False, "pose": False, "parse_errors": set()}

    def on_lowstate(msg):
        try:
            report["streams"]["lowstate"].update(extract_lowstate(msg))
            report["streams"]["lowstate"]["host_receipt_unix_s"] = time.time()
            state["lowstate"] = True
        except Exception as exc:  # malformed telemetry must not crash the probe
            state["parse_errors"].add(f"lowstate: {type(exc).__name__}")

    def on_pose(msg):
        try:
            report["streams"]["pose"].update(extract_pose(msg))
            report["streams"]["pose"]["host_receipt_unix_s"] = time.time()
            state["pose"] = True
        except Exception as exc:
            state["parse_errors"].add(f"pose: {type(exc).__name__}")

    async def on_track(track):
        try:
            frame = await asyncio.wait_for(
                track.recv(), max(0.1, deadline - loop.time())
            )
            width, height = getattr(frame, "width", None), getattr(frame, "height", None)
            if type(width) is not int or type(height) is not int or not (
                1 <= width <= 8192 and 1 <= height <= 8192
            ):
                raise ValueError("not a decoded video frame")
            report["streams"]["video"]["frame"] = {
                "width": width,
                "height": height,
                "host_receipt_unix_s": time.time(),
            }
            state["frame"] = True
        except Exception as exc:
            report["streams"]["video"]["error"] = f"{type(exc).__name__}"

    factory = conn_factory or _default_conn_factory
    async def establish():
        # The SDK creates video during connect(), and delivers its track once.
        # Register before connect completes so an early frame cannot be missed.
        task = asyncio.create_task(conn.connect())
        try:
            while getattr(conn, "video", None) is None and not task.done():
                await asyncio.sleep(0.005)
            if getattr(conn, "video", None) is not None:
                conn.video.add_track_callback(on_track)
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    try:
        conn = factory(ip, aes_key)
        await asyncio.wait_for(establish(), max(0.1, deadline - loop.time()))
        report["connection"]["status"] = "connected"
        dc = conn.datachannel
        # Vendor request can hang; keep it within the connection deadline.
        await asyncio.wait_for(
            dc.disableTrafficSaving(True), max(0.1, deadline - loop.time())
        )
        dc.pub_sub.subscribe(TOPICS["lowstate"], on_lowstate)
        dc.pub_sub.subscribe(TOPICS["pose"], on_pose)
        dc.switchVideoChannel(True)
        streams = report["streams"]
        while loop.time() < deadline and not (
            state["frame"] and state["lowstate"] and state["pose"]
        ):
            await asyncio.sleep(poll_interval)
        streams["video"]["received"] = bool(state["frame"])
        streams["lowstate"]["received"] = bool(state["lowstate"])
        streams["pose"]["received"] = bool(state["pose"])
        for pe in sorted(state["parse_errors"]):
            report["notes"].append("parse_error: " + pe)
    except Exception as exc:
        report["connection"]["status"] = (
            "timeout" if isinstance(exc, asyncio.TimeoutError) else "connect_failed"
        )
        report["connection"]["error"] = safe_error(exc)
    finally:
        if conn is not None:
            try:
                conn.datachannel.switchVideoChannel(False)
            except Exception:
                pass
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=5.0)
                report["connection"]["disconnected"] = True
            except Exception as exc:
                report["connection"]["disconnected"] = False
                report["connection"]["disconnect_error"] = safe_error(exc)
    report["completed"] = report["connection"].get("disconnected", False) and all(
        report["streams"][s]["received"] for s in ("video", "lowstate", "pose")
    )
    return report


def _child_main(args) -> int:
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # vendor logs can echo auth material
    aes_key = os.environ.get("UNITREE_AES_128_KEY")  # optional: only for newer firmware
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            report = asyncio.run(run_probe(args.ip, args.timeout, aes_key=aes_key))
    finally:
        logging.disable(previous_logging)
    payload = json.dumps(report)
    print(payload)
    return 0 if report["completed"] else 1


def _parent_main(args) -> int:
    cmd = [sys.executable, os.path.abspath(__file__), "--ip", args.ip,
           "--timeout", str(args.timeout), "--_child"]
    target = str(Path(args.output).resolve()) if args.output else None
    child_env = {k: v for k, v in os.environ.items()
                 if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    child_env["NO_PROXY"] = "*"  # Robot signaling must stay on the local connection.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=args.timeout + 5.0, env=child_env)
    except subprocess.TimeoutExpired:
        report = new_report(args.ip)
        report["connection"] = {"status": "timeout_hard"}
        if target:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(report) + "\n")
        print(json.dumps(report))
        return 1
    report = None
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict) and candidate.get("probe") == PROBE_VERSION:
                report = candidate
                break
        except ValueError:
            continue
    if report is None:
        # Never forward child stdout/stderr: raw exception text can carry secrets.
        print(
            f"go2-probe: child produced no JSON report (exit={proc.returncode}, "
            "child output withheld).",
            file=sys.stderr,
        )
        return 1
    if target:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report) + "\n")
    streams = report.get("streams", {})
    summary = ", ".join(f"{name}={'ok' if s.get('received') else 'MISSING'}"
                        for name, s in streams.items())
    print(f"go2-probe: {report.get('connection', {}).get('status')} ({summary})")
    print(json.dumps(report))
    return proc.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stationary read-only Go2 probe.")
    parser.add_argument("--ip", required=True, help="robot LAN IP literal")
    parser.add_argument("--timeout", type=float, default=20.0, help="5-60 s")
    parser.add_argument("--output", help="parent writes sanitized JSON report here")
    parser.add_argument("--_child", help=argparse.SUPPRESS, action="store_true")
    args = parser.parse_args(argv)
    if not 5.0 <= args.timeout <= 60.0:
        parser.error("--timeout must be 5-60 s")
    try:
        parsed_ip = ipaddress.ip_address(args.ip)
    except ValueError:
        parser.error("--ip must be a private IPv4 address literal")
    private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if parsed_ip.version != 4 or not any(parsed_ip in ipaddress.ip_network(n) for n in private_ranges):
        parser.error("--ip must be a private IPv4 address literal")
    return _child_main(args) if args._child else _parent_main(args)


if __name__ == "__main__":
    sys.exit(main())
