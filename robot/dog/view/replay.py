"""Replay a recorded patrol (spacetime JSONL) into the command-center page without the dog.

    .venv/bin/python robot/dog/view/replay.py --file .data/hardware/spacetime.jsonl --port 8012 --speed 2

Serves the same routes as the live dog process (`/`, `/telemetry.json`, `/spacetime*`, `/map.jpg`, `/stream.mjpg`)
from the recording, at wall-clock speed times `--speed`, looping. The camera panel shows a synthetic frame
(the recording holds no video; `--frames-dir` cycles JPEGs from a folder instead). Commands are accepted and
logged, nothing moves. For the page's design and the live wiring see `robot/dog/runtime/patrol.py`.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repository root

from robot.dog.memory.spacetime import SpacetimeRecorder  # noqa: E402


def load_records(path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                try:
                    d = json.loads(raw)
                    if isinstance(d, dict) and isinstance(d.get("t"), (int, float)) and d.get("type") in ("pose", "obstacles", "people", "event"):
                        out.append(d)
                except ValueError:
                    continue
    out.sort(key=lambda d: d["t"])
    return out


def synthetic_frame(text_lines, size=(640, 360)):
    """A cream frame with a few lines of text: the recording has no video. None without OpenCV (camera panel stays empty)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = np.full((size[1], size[0], 3), (241, 246, 244), dtype=np.uint8)  # BGR of #f4f6f7
    for i, line in enumerate(text_lines):
        cv2.putText(img, line, (24, 60 + 34 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.8 if i == 0 else 0.6, (100, 90, 69), 2 if i == 0 else 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


class Replay:
    def __init__(self, view, records, *, speed=1.0, frames_dir=None, grid_size_m=12.0):
        from robot.dog.planning.missions import MissionBoard
        from robot.dog.planning.smart_patrol import OccupancyGrid
        self.view, self.records, self.speed = view, records, max(0.1, float(speed))
        self.frames = sorted(Path(frames_dir).glob("*.jpg")) if frames_dir else []
        self.recorder = SpacetimeRecorder(min_frame_dt=0.0, pose_min_dt=0.0, people_min_dt=0.0, path=None)
        try:
            from robot.dog.memory.graph import SpacetimeGraph
            self.graph = SpacetimeGraph()
        except Exception:
            self.graph = None
        self.grid = OccupancyGrid((0.0, 0.0), size_m=grid_size_m)
        self.report = {"connection": {"status": "connected"}, "source": "replay", "reason": None, "elapsed_s": 0.0,
                       "brain": {"enabled": True, "decisions": [], "failures": 0}, "voice": {"commands": []},
                       "greetings": [], "checkins": [], "collisions": [], "frontier": {"available": False, "goals": 0}}
        view.recorder, view.graph, view.report, view.brain_period_s = self.recorder, self.graph, self.report, 6
        view.missions = MissionBoard()
        view.frontier = {"planner": None, "goal": None}
        view.commands = {"intent": None, "until": 0.0, "source": None}
        self.stop = threading.Event()

    def run(self):
        if not self.records:
            return
        t0_rec, t0_wall = self.records[0]["t"], time.monotonic()
        start_wall = t0_wall
        i, pose, people, mode, frame_i = 0, None, [], "cruise", 0
        while not self.stop.is_set():
            now_rec = t0_rec + (time.monotonic() - t0_wall) * self.speed
            while i < len(self.records) and self.records[i]["t"] <= now_rec:
                d = self.records[i]
                i += 1
                kind = d["type"]
                try:
                    if kind == "pose":
                        pose = (d["x"], d["y"], d["yaw"])
                        self.recorder.record_pose(d["t"], d["x"], d["y"], d["yaw"])
                        self.grid.visit(d["x"], d["y"])
                        if self.graph is not None:
                            self.graph.ingest_pose(d["t"], d["x"], d["y"], d["yaw"])
                    elif kind == "obstacles":
                        self.recorder.record_obstacles(d["t"], d["points"])
                        self.grid.observe(d["points"])
                        if self.graph is not None:
                            self.graph.ingest_obstacles(d["t"], d["points"])
                    elif kind == "people":
                        people = d["people"]
                        self.recorder.record_people(d["t"], people)
                        if self.graph is not None:
                            self.graph.ingest_people(d["t"], [dict(p, kind="person") for p in people])
                        for p in people:
                            self.grid.mark(p["x"], p["y"], "seen")
                    elif kind == "event":
                        self.recorder.record_event(d["t"], d["x"], d["y"], d["kind"], d["text"])
                        if self.graph is not None:
                            self.graph.ingest_event(d["t"], d["x"], d["y"], d["kind"], d["text"])
                        t_s = round(d["t"] - t0_rec, 1)
                        if d["kind"] == "greet":
                            self.report["greetings"].append({"t_s": t_s, "text": d["text"], "identity": None})
                            self.grid.mark(d["x"], d["y"], "greet")
                            self.view.log(f"t={t_s:5.1f}s greet: {d['text']}")
                        elif d["kind"] == "checkin":
                            self.report["checkins"].append({"t_s": t_s})
                            self.view.log(f"t={t_s:5.1f}s check-in: {d['text']}")
                        elif d["kind"] == "collision":
                            self.report["collisions"].append({"t_s": t_s, "mode": "cruise", "front_m": None})
                            self.view.log(f"t={t_s:5.1f}s COLLISION backoff")
                except Exception as exc:  # a bad record must not stop the replay
                    self.view.log(f"replay skipped a {kind} record: {type(exc).__name__}")
            if i >= len(self.records):  # loop
                i, t0_rec, t0_wall = 0, self.records[0]["t"], time.monotonic()
                self.recorder = SpacetimeRecorder(min_frame_dt=0.0, pose_min_dt=0.0, people_min_dt=0.0, path=None)
                self.view.recorder = self.recorder
                self.view.log("replay: looping from the start")
            elapsed = (time.monotonic() - start_wall) * self.speed
            self.report["elapsed_s"] = round(elapsed, 1)
            if int(elapsed) % 6 == 0 and (not self.report["brain"]["decisions"] or self.report["brain"]["decisions"][-1]["t_s"] < int(elapsed)):
                self.report["brain"]["decisions"].append({"t_s": float(int(elapsed)), "action": "explore", "seconds": 6,
                                                          "reason": "replay: decisions are not re-run from a recording"})
                self.report["brain"]["decisions"] = self.report["brain"]["decisions"][-50:]
            tracks = [{"track_id": p["track_id"], "posture": p.get("posture"), "identity": {"name": p["identity"]} if p.get("identity") else None,
                       "conf": 0.9, "box": [0, 0, 1, 1]} for p in people]
            mode = "follow" if tracks else "cruise"
            dist = math.hypot(pose[0], pose[1]) if pose else 0.0
            self.view.update(mode=mode, action="patrol", tracks=tracks, ranges={"front": float("inf"), "left": float("inf"), "right": float("inf")},
                             battery=100.0, t_s=elapsed, greetings=len(self.report["greetings"]), checkins=len(self.report["checkins"]),
                             home_m=dist, fps=14.0,
                             diag={"received_fps": 14.0, "processed_fps": 13.6, "control_hz": 9.8, "track_ms": {"mean": 31.0, "p95": 44.0},
                                   "convert_ms": {"mean": 4.0, "p95": 7.0}, "loop_lag_ms": {"mean": 9.0, "p95": 21.0},
                                   "voxel_ms": {"mean": 8.0, "p95": 15.0}, "objects_ms": {"mean": 27.0, "p95": 33.0}, "age_ms": {"mean": 42.0, "p95": 70.0}})
            try:
                if pose:
                    self.view.map_jpeg = self.grid.render(pose_xy=(pose[0], pose[1]), yaw=pose[2], palette="costmap")
                    self.view.map_geometry = self.grid.geometry()
            except Exception:
                pass
            if self.frames:
                data = self.frames[frame_i % len(self.frames)].read_bytes()
                frame_i += 1
            else:
                data = synthetic_frame(["Replay: recorded patrol, no video", f"t = {elapsed:6.1f} s   people in view: {len(people)}",
                                        f"pose {pose[0]:.2f}, {pose[1]:.2f} m  yaw {math.degrees(pose[2]):.0f} deg" if pose else "no pose yet"])
            if data:
                with self.view.new_frame:
                    self.view.jpeg = data
                    self.view.frame_seq += 1
                    self.view.new_frame.notify_all()
            time.sleep(0.2)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", default=".data/hardware/spacetime.jsonl")
    ap.add_argument("--port", type=int, default=8012)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--frames-dir", default=None, help="folder of JPEGs to cycle in the camera panel")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds to serve (0 = until Ctrl-C)")
    args = ap.parse_args(argv)
    from robot.dog.runtime.patrol import LiveView
    records = load_records(args.file)
    if not records:
        print(f"no records in {args.file}", file=sys.stderr)
        return 2
    view = LiveView(port=args.port, host=args.host)
    replay = Replay(view, records, speed=args.speed, frames_dir=args.frames_dir)
    url = view.start()
    print(f"command center (replay of {len(records)} records, x{args.speed}) -> {url}", flush=True)
    th = threading.Thread(target=replay.run, daemon=True)
    th.start()
    try:
        if args.duration > 0:
            time.sleep(args.duration)
        else:
            while th.is_alive():
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    replay.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
