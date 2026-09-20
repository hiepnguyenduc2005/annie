#!/usr/bin/env python3
"""Hardware perception loop for the Go2: camera + pose in, cited observations out.

Read-only with respect to motion: it never sends a sport, stand, or navigation
request. It streams the robot camera over the same WebRTC connection as
go2_probe.py, runs the tracked keypoint posture detector on every new frame,
sends the latest frame to the local brain's /infer on a bounded cadence (one
in flight, stale frames dropped, never re-dated), and publishes the resulting
perception plus a measured dog.status to the app backend with the frame's
capture identity (frame_id, host receipt ts, pose, map_id, source=hardware).

Capture timestamps are host receipt times; pose is the robot's odometry pose
paired by receipt time. Neither is calibrated sensor synchronization. Tracks
and postures are image-space estimates, not diagnoses. Frames never leave the
host except to the loopback brain URL; nothing here logs frame bytes.

Run from the repository root (dog reachable, key in the environment):
  .cache/dimos/.venv/bin/python robot/go2_perception.py --map-id henry-home \
      --brain-url http://127.0.0.1:8004 --app-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # robot.simulation imports when run as a script
from go2_probe import extract_lowstate, extract_pose, safe_error  # noqa: E402

SCRIPT_VERSION = "go2-perception/0.1"
TOPIC_LOWSTATE = "rt/lf/lowstate"
TOPIC_POSE = "rt/utlidar/robot_pose"
MAX_JPEG_BYTES = 1_000_000


def yaw_from_quaternion(q: dict) -> float:
    x, y, z, w = (float(q[k]) for k in "xyzw")
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def encode_frame(frame, longest_side: int = 640) -> bytes:
    """Encode one decoded WebRTC video frame to JPEG (real path only)."""
    import cv2

    image = frame.to_ndarray(format="bgr24")
    h, w = image.shape[:2]
    scale = longest_side / max(h, w)
    if scale < 1.0:
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buffer.tobytes()


def _default_conn_factory(ip: str, aes_key: str | None):
    from unitree_webrtc_connect.constants import WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=aes_key)


def _default_tracker():
    from robot.simulation.person_tracker import PersonTracker
    return PersonTracker()


def _say(text: str) -> None:
    print(f"go2-perception: {text}", file=sys.stderr, flush=True)


class _Telemetry:
    def __init__(self, clock, map_id):
        self.clock, self.map_id = clock, map_id
        self.soc = None
        self.low_t = None
        self.pose = None   # {x, y, yaw, map_id}
        self.pose_t = None
        self.parse_errors = set()

    def on_lowstate(self, msg):
        try:
            self.soc = extract_lowstate(msg)["battery"]["soc_percent"]
            self.low_t = self.clock()
        except Exception as exc:
            self.parse_errors.add(f"lowstate: {type(exc).__name__}")

    def on_pose(self, msg):
        try:
            data = extract_pose(msg)
            self.pose = {"x": data["position"]["x"], "y": data["position"]["y"],
                         "yaw": yaw_from_quaternion(data["orientation_xyzw"]), "map_id": self.map_id}
            self.pose_t = self.clock()
        except Exception as exc:
            self.parse_errors.add(f"pose: {type(exc).__name__}")

    def fresh(self, now, stale_s):
        return (self.low_t is not None and self.pose_t is not None
                and now - self.low_t <= stale_s and now - self.pose_t <= stale_s)


async def run_perception(*, ip: str, aes_key: str | None, map_id: str, brain_url: str, app_url: str,
                         conn_factory=None, tracker=None, encoder=encode_frame, http_transport=None,
                         rate_hz: float = 5.0, infer_interval_s: float = 1.0, stale_s: float = 2.0,
                         status_interval_s: float = 1.0, first_telemetry_s: float = 5.0,
                         connect_timeout_s: float = 20.0, duration_s: float | None = None,
                         status=_say) -> dict:
    loop = asyncio.get_running_loop()
    tel = _Telemetry(loop.time, map_id)
    report = {"script": SCRIPT_VERSION, "source": "hardware", "target_ip": ip, "map_id": map_id,
              "connection": {"status": "not_started"}, "frames": 0, "tracks_seen": 0,
              "max_lying_frames": 0, "inferences": 0, "inference_errors": 0, "ingest_accepted": 0,
              "ingest_rejected": 0, "last_perception": None, "reason": None, "completed": False,
              "notes": ["No motion commands sent; camera, telemetry and loopback inference only.",
                        "Host receipt timestamps; pose paired by receipt time, not calibrated sync."]}
    conn = None
    tracker = tracker if tracker is not None else _default_tracker()
    latest = {"frame": None, "t": None, "seq": 0}
    seen_tracks = set()
    infer_task = None
    stopped = False

    async def on_track(track):
        while not stopped:
            try:
                frame = await track.recv()
            except Exception:
                return
            latest["frame"], latest["t"], latest["seq"] = frame, loop.time(), latest["seq"] + 1

    async def infer(jpeg: bytes, capture_ts_ms: int, pose: dict):
        payload = {"frame_id": str(uuid4()), "ts": capture_ts_ms, "pose": pose, "source": "hardware",
                   "jpeg_b64": base64.b64encode(jpeg).decode()}
        try:
            async with httpx.AsyncClient(base_url=brain_url, timeout=30, trust_env=False,
                                         transport=http_transport, headers=_headers("ANNIE_BRAIN_TOKEN")) as brain:
                response = await brain.post("/infer", json=payload)
                response.raise_for_status()
                result = response.json()
            perception = result["perception"]
            if any(perception[key] != payload[key] for key in ("frame_id", "ts", "pose")):
                raise ValueError("inference evidence mismatch")
            report["inferences"] += 1
            report["last_perception"] = {k: perception[k] for k in
                                         ("frame_id", "ts", "person", "posture", "location", "confidence")}
            report["last_perception"]["caption"] = str(perception.get("caption", ""))[:200]
            accepted = await ingest("brain.perception", perception)
            status(f"perception person={perception['person']} posture={perception['posture']} "
                   f"location={perception['location']} conf={perception['confidence']:.2f} "
                   f"latency={result.get('latency_ms')} ms accepted={accepted}")
        except Exception as exc:
            report["inference_errors"] += 1
            report["last_inference_error"] = safe_error(exc)

    async def ingest(channel: str, data: dict) -> bool:
        try:
            async with httpx.AsyncClient(base_url=app_url, timeout=5, trust_env=False,
                                         transport=http_transport, headers=_headers("ANNIE_API_TOKEN")) as app:
                response = await app.post("/ingest", json={"channel": channel, "data": data})
                response.raise_for_status()
                accepted = response.json().get("accepted") is True
        except Exception as exc:
            report["ingest_rejected"] += 1
            report["last_ingest_error"] = safe_error(exc)
            return False
        if accepted:
            report["ingest_accepted"] += 1
        else:
            report["ingest_rejected"] += 1
        return accepted

    try:
        conn = conn_factory(ip, aes_key) if conn_factory else _default_conn_factory(ip, aes_key)
        try:
            task = asyncio.create_task(conn.connect())
            while getattr(conn, "video", None) is None and not task.done():
                await asyncio.sleep(0.005)
            if getattr(conn, "video", None) is not None:
                conn.video.add_track_callback(on_track)
            await asyncio.wait_for(task, connect_timeout_s)
        except Exception as exc:
            report["connection"]["status"] = "connect_failed"
            report["connection"]["error"] = safe_error(exc)
            report["reason"] = f"connect_failed:{type(exc).__name__}"
            return report
        report["connection"]["status"] = "connected"
        await asyncio.wait_for(conn.datachannel.disableTrafficSaving(True), connect_timeout_s)
        conn.datachannel.pub_sub.subscribe(TOPIC_LOWSTATE, tel.on_lowstate)
        conn.datachannel.pub_sub.subscribe(TOPIC_POSE, tel.on_pose)
        conn.datachannel.switchVideoChannel(True)

        deadline = loop.time() + first_telemetry_s
        while loop.time() < deadline and (tel.low_t is None or tel.pose_t is None):
            await asyncio.sleep(0.01)
        if tel.low_t is None or tel.pose_t is None:
            report["reason"] = "telemetry_missing"
            return report
        status(f"connected, battery {tel.soc:.0f}%, pose x={tel.pose['x']:.2f} y={tel.pose['y']:.2f}")

        start = loop.time()
        tick = 1.0 / rate_hz
        last_seq = 0
        last_infer = None
        last_status = None
        last_print = start
        while duration_s is None or loop.time() - start < duration_s:
            now = loop.time()
            if latest["seq"] != last_seq and tel.fresh(now, stale_s):
                last_seq = latest["seq"]
                frame_t = latest["t"]
                now_ms = int(time.time() * 1000)
                capture_ts_ms = now_ms - int((now - frame_t) * 1000)
                pose = dict(tel.pose)
                try:
                    jpeg = encoder(latest["frame"])
                    if len(jpeg) > MAX_JPEG_BYTES:
                        raise ValueError("encoded frame exceeds the brain size limit")
                    tracks = tracker.update(jpeg, now_ms=now_ms)
                except Exception as exc:
                    report["frame_error"] = safe_error(exc)
                    tracks, jpeg = [], None
                report["frames"] += 1
                for track in tracks:
                    if track.get("track_id") is not None:
                        seen_tracks.add(track["track_id"])
                    report["max_lying_frames"] = max(report["max_lying_frames"], int(track.get("lying_frames", 0)))
                report["tracks_seen"] = len(seen_tracks)
                if jpeg is not None and (infer_task is None or infer_task.done()) and \
                        (last_infer is None or now - last_infer >= infer_interval_s):
                    last_infer = now
                    infer_task = asyncio.create_task(infer(jpeg, capture_ts_ms, pose))
                if now - last_print >= 1.0:
                    last_print = now
                    lying = [t for t in tracks if t.get("posture") == "lying"]
                    status(f"t={now - start:5.1f}s frames={report['frames']} tracks={len(tracks)} "
                           f"lying={len(lying)} infers={report['inferences']} battery={tel.soc:.0f}%")
            if tel.fresh(now, stale_s) and (last_status is None or now - last_status >= status_interval_s):
                last_status = now
                await ingest("dog.status", {"ts": int(time.time() * 1000), "state": "idle",
                                            "battery_pct": float(tel.soc), "waypoint": None, "pose": dict(tel.pose)})
            await asyncio.sleep(tick)
        report["reason"] = "duration_complete" if duration_s is not None else "stopped"
    except asyncio.CancelledError:
        report["reason"] = "operator_cancelled"
    except Exception as exc:
        report["reason"] = f"error:{type(exc).__name__}"
        report["error"] = safe_error(exc)
    finally:
        stopped = True
        if infer_task is not None and not infer_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(infer_task, 5.0)
        if conn is not None and report["connection"]["status"] == "connected":
            with contextlib.suppress(Exception):
                conn.datachannel.switchVideoChannel(False)
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=5.0)
                report["connection"]["disconnected"] = True
            except Exception as exc:
                report["connection"]["disconnected"] = False
                report["connection"]["disconnect_error"] = safe_error(exc)
        for pe in sorted(tel.parse_errors):
            report["notes"].append("parse_error: " + pe)
        report["completed"] = (report["reason"] in ("duration_complete", "stopped", "operator_cancelled")
                               and report["frames"] > 0 and report["connection"].get("disconnected", False))
    return report


def _headers(name: str) -> dict:
    token = os.environ.get(name) or (os.environ.get("ANNIE_API_TOKEN") if name == "ANNIE_BRAIN_TOKEN" else None)
    return {"Authorization": f"Bearer {token}"} if token else {}


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
    parser = argparse.ArgumentParser(description="Go2 hardware perception loop (camera + pose in, cited observations out).")
    parser.add_argument("--ip", type=_private_ipv4, default="192.168.12.1")
    parser.add_argument("--map-id", default="henry-home")
    parser.add_argument("--brain-url", default="http://127.0.0.1:8004")
    parser.add_argument("--app-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rate", type=float, default=5.0, help="tracker frames per second")
    parser.add_argument("--infer-interval", type=float, default=1.0, help="seconds between brain calls")
    parser.add_argument("--duration", type=float, help="stop after this many seconds (default: until Ctrl-C)")
    parser.add_argument("--output", help="write the JSON report here")
    args = parser.parse_args(argv)
    if not 0.5 <= args.rate <= 30 or not 0.2 <= args.infer_interval <= 60:
        parser.error("--rate must be 0.5-30 and --infer-interval 0.2-60")
    for key in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    logging.disable(logging.CRITICAL)  # vendor logs can echo auth material
    aes_key = os.environ.get("UNITREE_AES_128_KEY")
    try:
        report = asyncio.run(run_perception(ip=args.ip, aes_key=aes_key, map_id=args.map_id,
                                            brain_url=args.brain_url, app_url=args.app_url,
                                            rate_hz=args.rate, infer_interval_s=args.infer_interval,
                                            duration_s=args.duration))
    except KeyboardInterrupt:
        _say("interrupted before the run could report")
        return 130
    payload = json.dumps(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    _say(f"{report['reason']} frames={report['frames']} tracks={report['tracks_seen']} "
         f"infers={report['inferences']} accepted={report['ingest_accepted']} errors={report['inference_errors']}")
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
