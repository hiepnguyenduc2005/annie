"""HTTP adapter for the simulated body. Battery 100% is a synthetic placeholder.

Run: .venv/bin/python simulation/bridge.py --once
Vision is opt-in and bounded; authored ground truth is a distinct demo mode.
Speech completion requires a browser playback receipt; no hardware execution.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from collections import OrderedDict
from uuid import UUID, uuid4

import httpx

LOG = logging.getLogger("annie.simulation.bridge")
TERMINAL = {"completed", "failed"}


class Bridge:
    def __init__(self, viewer, app, brain, *, perception="disabled", max_inferences=20,
                 inference_interval=2.0, clock=time.time, monotonic=time.monotonic, status_file=None):
        self.viewer, self.app, self.brain = viewer, app, brain
        self.perception = perception
        self.max_inferences = max_inferences
        self.inference_interval = inference_interval
        self.clock, self.monotonic = clock, monotonic
        self.map_id = None
        self.commands = OrderedDict()
        self.inferences = 0
        self.next_inference = 0.0
        self.vision_task = None
        self.errors = {}
        self.frames = OrderedDict()
        self.status_file = Path(status_file) if status_file else None
        self.last_perception = self.last_provider = self.last_latency_ms = None
        self.last_error = self.ingest_accepted = None

    def write_status(self):
        if self.status_file is None:
            return
        status = dict(updated_at=int(self.clock() * 1000), perception_mode=self.perception,
            context_map_id=self.map_id,
            inference_limit_reached=self.perception == 'vision' and self.inferences >= self.max_inferences,
            inferences=self.inferences, max_inferences=self.max_inferences,
            last_perception=self.last_perception, last_provider=self.last_provider,
            last_latency_ms=self.last_latency_ms, last_error=self.last_error,
            ingest_accepted=self.ingest_accepted)
        temporary = None
        try:
            encoded = json.dumps(status, allow_nan=False)
            if len(encoded.encode()) > 16000:
                raise ValueError("Status exceeds bounded size")
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.status_file.parent,
                                             prefix=".bridge-status-", delete=False) as handle:
                temporary = handle.name
                handle.write(encoded)
            os.replace(temporary, self.status_file)
        except (OSError, ValueError) as exc:
            self.warn("status file", exc)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def record_perception(self, data):
        # Explicit image-free projection; never serialize raw provider responses.
        self.last_perception = {k: data[k] for k in
            ("ts", "frame_id", "person", "posture", "location", "confidence")}
        self.last_perception["caption"] = str(data["caption"])[:2000]
        for key in ("source", "model"):
            if key in data:
                self.last_perception[key] = str(data[key])[:200] if data[key] is not None else None
        self.last_perception["pose"] = {k: data["pose"][k] for k in ("x", "y", "yaw", "map_id")}


    def warn(self, area, exc):
        # Never log response bodies, images, URLs, tokens or provider exception text.
        self.last_error = f"{area} unavailable ({type(exc).__name__})"
        now = self.monotonic()
        if now - self.errors.get(area, -float("inf")) >= 30:
            LOG.warning("%s unavailable (%s)", area, type(exc).__name__)
            self.errors[area] = now

    async def request(self, client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def ingest(self, channel, data):
        return await self.request(self.app, "POST", "/ingest", json={"channel": channel, "data": data})

    async def receipt(self, command_id, status, detail=None):
        await self.request(self.app, "POST", f"/commands/{command_id}/receipt", json={
            "status": status, "source": "simulation", "detail": detail})

    @staticmethod
    def pose(state):
        x, y, z, w, qx, qy, qz = state["qpos_base"]
        if not all(math.isfinite(v) for v in (x, y, z, w, qx, qy, qz)):
            raise ValueError("Non-finite body pose")
        return {"x": x, "y": y, "yaw": math.atan2(2 * (w*qz + qx*qy),
                1 - 2 * (qy*qy + qz*qz)), "map_id": state["map_id"]}

    async def body(self, state):
        if not state.get("ready"):
            return
        pose = self.pose(state)
        nav = state["navigation"]
        ts = int(self.clock() * 1000)
        scene = state.get("current_scene") or {}
        if self.map_id != state["map_id"]:
            self.last_perception = self.last_latency_ms = self.ingest_accepted = None
            size = (scene.get("ground_truth") or {}).get("room_size_m")
            origin = {"x": -size[0]/2, "y": -size[1]/2} if size else {"x": 0, "y": 0}
            rooms = scene.get("rooms", [{"id": "home", "label": "Synthetic home",
                **origin, "width": size[0], "height": size[1]}] if size else [])
            await self.ingest("dog.map", {"ts": ts, "map_id": state["map_id"],
                "origin": origin, "resolution_m": 0.05,
                "waypoints": nav["waypoints"], "rooms": rooms})
            self.map_id = state["map_id"]
        activity = "paused" if (state.get("physics_error") or not state["running"] or
                    nav["state"] in {"stopped", "failed"}) else (
                    "patrolling" if nav["state"] == "moving" else "idle")
        await self.ingest("dog.status", {"ts": ts, "state": activity,
            "battery_pct": 100, "waypoint": nav.get("waypoint"), "pose": pose})
        await self.process_commands(nav)
        if self.perception == "ground-truth":
            truth = scene.get("ground_truth")
            if truth is not None and self.monotonic() >= self.next_inference:
                self.next_inference = self.monotonic() + self.inference_interval
                perception = {"ts": ts, "frame_id": str(uuid4()),
                    "source": "simulation_ground_truth",
                    "person": bool(truth["resident_present"]),
                    "posture": {"seated": "sitting"}.get(truth.get("posture"),
                        truth.get("posture")) or "unknown",
                    "location": truth.get("support_surface") or "unknown",
                    "confidence": 1.0, "pose": pose,
                    "caption": "GROUND TRUTH: authored simulator labels; not camera inference."}
                self.record_perception(perception)
                self.last_provider = {"mode": "ground-truth", "model": "authored simulator labels"}
                self.last_latency_ms = None
                self.ingest_accepted = None
                response = await self.ingest("brain.perception", perception)
                self.ingest_accepted = response.get("accepted") if isinstance(response.get("accepted"), bool) else None
                self.last_error = None

    async def process_commands(self, nav):
        queued = await self.request(self.app, "GET", "/commands")
        results = {item["command_id"]: item for item in nav.get("commands", [])}
        current_ids = {item["command_id"] for item in queued}
        # The app retains 100 commands. Retain outstanding entries plus its window.
        for key in list(self.commands):
            if key not in current_ids:
                del self.commands[key]
        for item in queued:
            cid = str(UUID(item["command_id"]))
            if item["status"] in TERMINAL:
                continue
            local = self.commands.get(cid)
            result = results.get(cid)
            if local is None:
                local = self.commands[cid] = {"sent": item["status"] != "queued" or result is not None,
                                               "status": item["status"]}
            if not local["sent"]:
                # Mark before sending: uncertain transport outcomes must not replay motion.
                local["sent"] = True
                if item["cmd"] not in {"goto", "stop", "resume", "look", "patrol", "say"}:
                    local["pending"] = ("failed", "Unsupported simulation command.")
                else:
                    payload = {"action": "mission", "cmd": item["cmd"], "command_id": cid}
                    if item["cmd"] == "goto":
                        payload["waypoint"] = item["waypoint"]
                    try:
                        if item["cmd"] == "say":
                            await self.request(self.viewer, "POST", "/say",
                                json={"text": item["text"], "command_id": cid})
                        else:
                            await self.request(self.viewer, "POST", "/control", json=payload)
                        local["pending"] = ("accepted", "Queued by simulator; execution pending.")
                    except httpx.HTTPStatusError:
                        local["pending"] = ("failed", "Simulator rejected command.")
                    except httpx.RequestError:
                        # A timeout may occur after acceptance. Await identified viewer result.
                        local["pending"] = ("accepted", "Delivery uncertain; awaiting simulator result.")
            if "pending" in local:
                status, detail = local["pending"]
                await self.receipt(cid, status, detail)
                local["status"] = status
                del local["pending"]
            if result and result["status"] != local["status"] and local["status"] not in TERMINAL:
                status = result["status"]
                if status not in {"accepted", "executing", "completed", "failed"}:
                    continue
                if local["status"] == "queued" and status == "completed":
                    await self.receipt(cid, "accepted", "Identified simulator command result.")
                await self.receipt(cid, status, "Simulator reported " + status + ".")
                local["status"] = status

    async def infer(self):
        if self.inferences >= self.max_inferences:
            return
        try:
            observation = await self.request(self.viewer, "GET", "/observation")
            if not 0 <= self.clock() * 1000 - observation['ts'] <= 5000:
                self.last_error = 'Camera frame is stale or future-dated; inference skipped'
                return
            frame_id = observation["frame_id"]
            if frame_id in self.frames:
                return
            self.frames[frame_id] = True
            if len(self.frames) > 256:
                self.frames.popitem(last=False)
            payload = {key: observation[key] for key in ("frame_id", "ts", "pose", "source", "jpeg_b64")}
            self.inferences += 1  # Count attempts, including provider failures.
            if self.inferences == self.max_inferences:
                LOG.info("Vision inference budget reached (%d); body bridge continues.", self.max_inferences)
            result = await self.request(self.brain, "POST", "/infer", json=payload)
            perception = result["perception"]
            if any(perception[key] != payload[key] for key in ("frame_id", "ts", "pose")):
                raise ValueError("Inference evidence mismatch")
            if self.map_id and payload['pose']['map_id'] != self.map_id:
                self.ingest_accepted = False
                self.last_error = 'Inference belongs to a previous scene'
                return
            self.record_perception(perception)
            provider = result.get("provider") or {}
            self.last_provider = {k: str(provider[k])[:200] for k in ("mode", "model") if k in provider}
            if isinstance(provider.get('usage'), dict):
                self.last_provider['usage'] = {k: v for k, v in provider['usage'].items()
                    if k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cost_usd')
                    and type(v) in (int, float) and math.isfinite(v) and v >= 0}
            latency = result.get("latency_ms")
            self.last_latency_ms = latency if isinstance(latency, (int, float)) and math.isfinite(latency) else None
            self.ingest_accepted = None
            response = await self.ingest("brain.perception", perception)
            self.ingest_accepted = response.get("accepted") if isinstance(response.get("accepted"), bool) else None
            self.last_error = None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self.warn("vision", exc)
        finally:
            self.write_status()

    async def tick(self, *, wait_vision=False):
        try:
            state = await self.request(self.viewer, "GET", "/state")
            await self.body(state)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self.warn("body/app", exc)
            self.write_status()
            return
        if (state.get("ready") and self.perception == "vision" and
                self.inferences < self.max_inferences and self.monotonic() >= self.next_inference and
                (self.vision_task is None or self.vision_task.done())):
            self.next_inference = self.monotonic() + self.inference_interval
            self.vision_task = asyncio.create_task(self.infer())
        if wait_vision and self.vision_task:
            await self.vision_task
        self.write_status()


async def run(args):
    def headers(name):
        token = os.environ.get(name)
        if name == "ANNIE_BRAIN_TOKEN" and not token:
            token = os.environ.get("ANNIE_API_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=args.viewer_url, timeout=5, trust_env=False) as viewer, \
            httpx.AsyncClient(base_url=args.app_url, headers=headers("ANNIE_API_TOKEN"),
                              timeout=5, trust_env=False) as app, \
            httpx.AsyncClient(base_url=args.brain_url, headers=headers("ANNIE_BRAIN_TOKEN"),
                              timeout=45, trust_env=False) as brain:
        bridge = Bridge(viewer, app, brain, perception=args.perception,
                        max_inferences=args.max_inferences, inference_interval=args.inference_interval,
                        status_file=args.status_file)
        LOG.info("Simulation bridge: perception=%s; battery=100%% synthetic placeholder; inference cap=%d",
                 args.perception, args.max_inferences)
        try:
            while True:
                await bridge.tick(wait_vision=args.once)
                if args.once:
                    break
                await asyncio.sleep(args.poll_interval)
        finally:
            if bridge.vision_task and not bridge.vision_task.done():
                bridge.vision_task.cancel()
                await asyncio.gather(bridge.vision_task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer-url", default="http://127.0.0.1:8766")
    parser.add_argument("--app-url", default="http://127.0.0.1:8000")
    parser.add_argument("--brain-url", default="http://127.0.0.1:8002")
    parser.add_argument("--perception", choices=("disabled", "vision", "ground-truth"), default="disabled")
    parser.add_argument("--max-inferences", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--inference-interval", type=float, default=2)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status-file", default=".data/simulation/bridge-status.json",
                        help="Atomic image-free status JSON; empty string disables writing")
    args = parser.parse_args()
    if args.max_inferences < 0 or not all(math.isfinite(x) and x > 0 for x in
                                        (args.poll_interval, args.inference_interval)):
        parser.error("inference cap must be nonnegative and intervals finite and positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
