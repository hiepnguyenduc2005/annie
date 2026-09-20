#!/usr/bin/env python3
"""Follow the person in front of the physical Go2 using our own tracker.

Camera frames come over WebRTC, the keypoint person tracker picks the target
(nearest = largest box at start, then that track_id), and a proportional
controller keeps the box centred (yaw) and at a target height (distance) by
streaming Move at 10 Hz. It stops when the person is too close, lost for more
than a second, the battery hits the floor, telemetry goes stale, the duration
elapses, or Ctrl-C. A supervised commissioning tool: cleared area, operator
nearby. Every stop is a software StopMove, not a hardware emergency stop, and
the host cannot stop the robot over a lost link.
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_probe import extract_lowstate, safe_error  # noqa: E402
from go2_walk import (MIN_OPERATING_SOC_PERCENT, MOTION_INHIBIT_PATH, TOPIC_LOWSTATE, TOPIC_SPORT,  # noqa: E402
                      _private_ipv4, _status_code)

SCRIPT_VERSION = "go2-follow/0.1"
STAND_UP, BALANCE_STAND, STOP_MOVE, MOVE = 1004, 1002, 1003, 1008


def follow_command(box, frame_w, frame_h, *, target_height_frac=0.55, too_close_frac=0.85,
                   max_vx=0.4, max_wz=0.8, k_yaw=1.6, k_dist=1.2):
    """Body velocity (vx, wz) that centres the box and holds a target size.

    Returns (vx, wz, reason). reason is 'ok', 'too_close' (stop), or 'centered'.
    Pure function so it can be unit-tested without hardware.
    """
    x1, y1, x2, y2 = box
    cx = (x1 + x2) / 2.0
    height_frac = (y2 - y1) / float(frame_h)
    if height_frac >= too_close_frac:
        return 0.0, 0.0, "too_close"
    err_x = (cx - frame_w / 2.0) / (frame_w / 2.0)  # -1 left .. +1 right
    wz = max(-max_wz, min(max_wz, -k_yaw * err_x))  # positive wz turns left
    err_d = target_height_frac - height_frac        # positive: person is far
    vx = max(0.0, min(max_vx, k_dist * err_d))
    if abs(err_x) > 0.5:
        vx = 0.0  # turn first, then walk
    return vx, wz, "ok"


def _default_conn_factory(ip, aes_key):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key)


def _say(text):
    print(f"go2-follow: {text}", file=sys.stderr, flush=True)


async def run_follow(*, ip, aes_key, conn_factory=None, tracker=None, encoder=None, duration_s=120.0,
                     rate_hz=10.0, lost_s=1.5, stale_s=1.0, min_soc=MIN_OPERATING_SOC_PERCENT,
                     first_frame_s=10.0, status=_say, **gains):
    loop = asyncio.get_running_loop()
    report = {"script": SCRIPT_VERSION, "source": "hardware", "target_ip": ip, "connection": {"status": "not_started"},
              "battery_soc_start": None, "target_track": None, "frames": 0, "moves_sent": 0, "elapsed_s": 0.0,
              "reason": None, "completed": False, "stop": {"requested": False, "ack_ms": None, "code": None},
              "notes": ["Software StopMove only; host cannot stop the robot over a lost link.",
                        "Tracker boxes are image estimates; target selection is largest box at start."]}
    if tracker is None:
        from robot.simulation.person_tracker import PersonTracker
        tracker = PersonTracker(conf=0.35)
    if encoder is None:
        import cv2

        def encoder(frame):
            img = frame.to_ndarray(format="bgr24")
            h, w = img.shape[:2]
            if w > 640:
                img = cv2.resize(img, (640, int(h * 640 / w)))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes(), img.shape[1], img.shape[0]
    tel = {"soc": None, "low_t": None}
    latest = {"frame": None, "seq": 0}
    stopped = False
    conn = None
    seq = 0

    def on_low(msg):
        try:
            tel["soc"], tel["low_t"] = extract_lowstate(msg)["battery"]["soc_percent"], loop.time()
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
        conn = conn_factory(ip, aes_key) if conn_factory else _default_conn_factory(ip, aes_key)
        task = asyncio.create_task(conn.connect())
        while getattr(conn, "video", None) is None and not task.done():
            await asyncio.sleep(0.005)
        if getattr(conn, "video", None) is not None:
            conn.video.add_track_callback(on_track)
        await asyncio.wait_for(task, 20)
        report["connection"]["status"] = "connected"
        await asyncio.wait_for(conn.datachannel.disableTrafficSaving(True), 10)
        conn.datachannel.pub_sub.subscribe(TOPIC_LOWSTATE, on_low)
        conn.datachannel.switchVideoChannel(True)
        deadline = loop.time() + first_frame_s
        while loop.time() < deadline and (latest["frame"] is None or tel["low_t"] is None):
            await asyncio.sleep(0.02)
        if latest["frame"] is None or tel["low_t"] is None:
            report["reason"] = "camera_or_telemetry_missing"
            return report
        report["battery_soc_start"] = tel["soc"]
        if tel["soc"] < min_soc:
            report["reason"] = "battery_low"
            return report
        status(f"connected, battery {tel['soc']:.0f}%, camera live; looking for a person")

        commanded = True
        for api in (STAND_UP, BALANCE_STAND):
            code = await request(api)
            if code not in (0, None):
                report["reason"] = f"stand_failed:{code}"
                return report
            await asyncio.sleep(1.5)

        start = loop.time()
        last_seq = 0
        target = None
        last_seen = None
        last_print = start
        tick = 1.0 / rate_hz
        while loop.time() - start < duration_s:
            now = loop.time()
            report["elapsed_s"] = round(now - start, 1)
            if tel["soc"] is not None and tel["soc"] < min_soc:
                report["reason"] = "battery_low"
                return report
            if now - tel["low_t"] > stale_s:
                report["reason"] = "telemetry_stale"
                return report
            if latest["seq"] != last_seq:
                last_seq = latest["seq"]
                jpeg, fw, fh = encoder(latest["frame"])
                tracks = tracker.update(jpeg, now_ms=int(time.time() * 1000))
                report["frames"] += 1
                if target is None and tracks:
                    biggest = max(tracks, key=lambda t: (t["box"][3] - t["box"][1]))
                    target = biggest.get("track_id")
                    report["target_track"] = target
                    status(f"following track {target} (box height {(biggest['box'][3]-biggest['box'][1])/fh:.2f} of frame)")
                match = next((t for t in tracks if t.get("track_id") == target), None) if target is not None else None
                if match is not None:
                    last_seen = now
                    vx, wz, why = follow_command(match["box"], fw, fh, **gains)
                    if why == "too_close":
                        send_move(0.0, 0.0)
                    else:
                        send_move(vx, wz)
                    if now - last_print >= 1.0:
                        last_print = now
                        status(f"t={now-start:5.1f}s track={target} h={(match['box'][3]-match['box'][1])/fh:.2f} "
                               f"vx={vx:.2f} wz={wz:.2f} {why} battery={tel['soc']:.0f}%")
            if target is not None and last_seen is not None and now - last_seen > lost_s:
                send_move(0.0, 0.0)
                if now - last_print >= 1.0:
                    last_print = now
                    status(f"t={now-start:5.1f}s target lost; holding")
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
                report["connection"]["disconnect_error"] = safe_error(exc)
        report["completed"] = report["reason"] in ("duration_complete", "operator_cancelled") and \
            report["connection"].get("disconnected", False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Follow the person in front of the Go2 (supervised).")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--max-speed", type=float, default=0.4, help="m/s cap, at most 0.6")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not 0.05 <= args.max_speed <= 0.6:
        parser.error("--max-speed must be 0.05-0.6")
    if MOTION_INHIBIT_PATH.exists():
        _say("physical motion inhibited by the active hardware task")
        return 2
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_follow(ip=args.ip, aes_key=os.environ.get("UNITREE_AES_128_KEY"),
                                        duration_s=args.duration, max_vx=args.max_speed))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload, flush=True)
    _say(f"{report['reason']} frames={report['frames']} moves={report['moves_sent']} stop_ack={report['stop']['ack_ms']} ms")
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0 if report["completed"] else 1)


if __name__ == "__main__":
    main()
