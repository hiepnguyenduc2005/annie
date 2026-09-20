#!/usr/bin/env python3
"""Firmware tricks on the physical Go2: stand, sit, hello, stretch, dance, flips.

These are the robot's own sport-mode behaviours (MCF api ids, firmware 1.1.7+),
sent over the same WebRTC path as go2_walk.py. The script stands the dog up,
plays each requested trick, waits for it to settle, and ends with a priority
StopMove. It is a supervised commissioning tool: a nearby operator, a cleared
area, and the same battery floor as walking. Tiers:

- gentle: stand, sit, rise, hello, stretch, heart, pose, content, scrape, stand_down
- dynamic: dance1, dance2, front_jump, front_pounce (needs 1 m of clear floor)
- acrobatic: front_flip, back_flip, left_flip, handstand (needs --allow-acrobatic,
  battery >= 60%, and 2 m of clear floor on every side; the firmware itself may
  refuse with a non-zero code)

A non-zero firmware code aborts the sequence. Nothing here proves the trick was
performed: the report records acknowledgment codes and telemetry, not motion.
The host-side stop cannot reach the robot over a lost link.

Run from the repository root:
  .cache/dimos/.venv/bin/python robot/go2_tricks.py --ip 172.20.10.10 hello stretch dance1
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from go2_probe import extract_lowstate, extract_pose, safe_error  # noqa: E402
from go2_walk import (MIN_OPERATING_SOC_PERCENT, MOTION_INHIBIT_PATH, TOPIC_LOWSTATE, TOPIC_POSE,  # noqa: E402
                      TOPIC_SPORT, _private_ipv4, _status_code)

SCRIPT_VERSION = "go2-tricks/0.1"
ACROBATIC_MIN_SOC = 60.0
MAX_TILT_RAD = 0.35

# name -> (MCF api id, tier). Ids from unitree_webrtc_connect SPORT_CMD_MCF.
TRICKS: dict[str, tuple[int, str]] = {
    "stand": (1004, "gentle"), "stand_down": (1005, "gentle"), "recover": (1006, "gentle"),
    "sit": (1009, "gentle"), "rise": (1010, "gentle"), "hello": (1016, "gentle"),
    "stretch": (1017, "gentle"), "content": (1020, "gentle"), "pose": (1028, "gentle"),
    "scrape": (1029, "gentle"), "heart": (1036, "gentle"),
    "dance1": (1022, "dynamic"), "dance2": (1023, "dynamic"),
    "front_jump": (1031, "dynamic"), "front_pounce": (1032, "dynamic"),
    "front_flip": (1030, "acrobatic"), "backflip": (2043, "acrobatic"),
    "left_flip": (2041, "acrobatic"), "handstand": (2044, "acrobatic"),
}
# Seconds to let the firmware finish before the next command (measured
# conservatively; the firmware gives no completion event over WebRTC).
SETTLE_S = {"gentle": 4.0, "dynamic": 8.0, "acrobatic": 6.0}
STAND_UP, BALANCE_STAND, STOP_MOVE = 1004, 1002, 1003


def resolve(names: list[str]) -> list[tuple[str, int, str]]:
    out = []
    for name in names:
        if name not in TRICKS:
            raise ValueError(f"unknown trick {name!r}; choose from {', '.join(sorted(TRICKS))}")
        api_id, tier = TRICKS[name]
        out.append((name, api_id, tier))
    return out


def _default_conn_factory(ip: str, aes_key: str | None):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key)


def _say(text: str) -> None:
    print(f"go2-tricks: {text}", file=sys.stderr, flush=True)


async def run_tricks(names: list[str], *, ip: str, aes_key: str | None, conn_factory=None,
                     allow_acrobatic: bool = False, settle_s: float | None = None,
                     first_telemetry_s: float = 5.0, stale_s: float = 1.0,
                     connect_timeout_s: float = 20.0, status=_say) -> dict:
    loop = asyncio.get_running_loop()
    sequence = resolve(names)
    report = {"script": SCRIPT_VERSION, "source": "hardware", "target_ip": ip,
              "requested": [n for n, _, _ in sequence], "performed": [],
              "connection": {"status": "not_started"}, "battery_soc_start": None,
              "reason": None, "completed": False,
              "notes": ["Codes are firmware acknowledgments, not evidence the trick happened.",
                        "Host-side stop cannot reach the robot over a lost link."]}
    if any(tier == "acrobatic" for _, _, tier in sequence) and not allow_acrobatic:
        report["reason"] = "acrobatic_not_allowed"
        return report
    tel = {"soc": None, "low_t": None, "rpy": None, "pose_t": None}

    def on_low(msg):
        try:
            data = extract_lowstate(msg)
            tel["soc"], tel["rpy"], tel["low_t"] = data["battery"]["soc_percent"], data["imu_rpy_rad"], loop.time()
        except Exception:
            pass

    def on_pose(msg):
        try:
            extract_pose(msg)
            tel["pose_t"] = loop.time()
        except Exception:
            pass

    conn = None
    commanded = False

    async def request(api_id: int, priority: bool = False, timeout: float = 5.0):
        options = {"api_id": api_id}
        if priority:
            options["priority"] = 1
        resp = await asyncio.wait_for(conn.datachannel.pub_sub.publish_request_new(TOPIC_SPORT, options), timeout)
        return _status_code(resp)

    try:
        conn = conn_factory(ip, aes_key) if conn_factory else _default_conn_factory(ip, aes_key)
        try:
            await asyncio.wait_for(conn.connect(), connect_timeout_s)
        except Exception as exc:
            report["connection"] = {"status": "connect_failed", "error": safe_error(exc)}
            report["reason"] = f"connect_failed:{type(exc).__name__}"
            return report
        report["connection"]["status"] = "connected"
        await asyncio.wait_for(conn.datachannel.disableTrafficSaving(True), connect_timeout_s)
        conn.datachannel.pub_sub.subscribe(TOPIC_LOWSTATE, on_low)
        conn.datachannel.pub_sub.subscribe(TOPIC_POSE, on_pose)
        deadline = loop.time() + first_telemetry_s
        while loop.time() < deadline and (tel["low_t"] is None or tel["pose_t"] is None):
            await asyncio.sleep(0.01)
        if tel["low_t"] is None or tel["pose_t"] is None:
            report["reason"] = "telemetry_missing"
            return report
        report["battery_soc_start"] = tel["soc"]
        if tel["soc"] < MIN_OPERATING_SOC_PERCENT:
            report["reason"] = "battery_low"
            return report
        if any(tier == "acrobatic" for _, _, tier in sequence) and tel["soc"] < ACROBATIC_MIN_SOC:
            report["reason"] = "battery_low_for_acrobatic"
            return report
        if max(abs(tel["rpy"][0]), abs(tel["rpy"][1])) > MAX_TILT_RAD:
            report["reason"] = "robot_not_level"
            return report
        status(f"connected, battery {tel['soc']:.0f}%, level; tricks: {', '.join(n for n, _, _ in sequence)}")

        commanded = True
        for api_id, label in ((STAND_UP, "StandUp"), (BALANCE_STAND, "BalanceStand")):
            code = await request(api_id)
            if code not in (0, None):
                report["reason"] = f"stand_failed:{code}"
                return report
            await asyncio.sleep(settle_s if settle_s is not None else 3.0)
        for name, api_id, tier in sequence:
            now = loop.time()
            if now - tel["low_t"] > stale_s or now - tel["pose_t"] > stale_s:
                report["reason"] = "telemetry_stale"
                return report
            status(f"{name} ({tier}, api {api_id})")
            t0 = loop.time()
            try:
                code = await request(api_id, timeout=8.0)
            except asyncio.TimeoutError:
                code = "no_ack"  # long behaviours (dance, jumps) do not ack within the window; the request was sent
            entry = {"name": name, "api_id": api_id, "code": code, "duration_s": round(loop.time() - t0, 3)}
            report["performed"].append(entry)
            if code not in (0, None, "no_ack"):
                report["reason"] = f"trick_rejected:{name}:{code}"
                return report
            await asyncio.sleep(settle_s if settle_s is not None else SETTLE_S[tier])
        report["reason"] = "sequence_complete"
    except asyncio.CancelledError:
        report["reason"] = "operator_cancelled"
    except Exception as exc:
        report["reason"] = f"error:{type(exc).__name__}"
        report["error"] = safe_error(exc)
    finally:
        if conn is not None and report["connection"]["status"] == "connected":
            if commanded:
                with contextlib.suppress(Exception):
                    code = await request(STOP_MOVE, priority=True, timeout=2.0)
                    report["stop_code"] = code
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=5.0)
                report["connection"]["disconnected"] = True
            except Exception as exc:
                report["connection"]["disconnected"] = False
                report["connection"]["disconnect_error"] = safe_error(exc)
        report["completed"] = (report["reason"] == "sequence_complete"
                               and report["connection"].get("disconnected", False))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Firmware tricks on the physical Go2 (supervised).")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--allow-acrobatic", action="store_true", help="permit flips and handstand")
    parser.add_argument("--settle", type=float, help="override the per-trick settle seconds")
    parser.add_argument("--output", help="write the JSON report here")
    parser.add_argument("tricks", nargs="+", help="e.g. hello stretch dance1")
    args = parser.parse_args(argv)
    try:
        resolve(args.tricks)
    except ValueError as exc:
        parser.error(str(exc))
    if MOTION_INHIBIT_PATH.exists():
        _say("physical motion inhibited by the active hardware task; no connection opened")
        return 2
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_tricks(args.tricks, ip=args.ip, aes_key=os.environ.get("UNITREE_AES_128_KEY"),
                                        allow_acrobatic=args.allow_acrobatic, settle_s=args.settle))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    _say(f"{report['reason']} performed={[t['name'] + ':' + str(t['code']) for t in report['performed']]}")
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
