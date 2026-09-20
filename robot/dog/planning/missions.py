"""Mission board: the body-command surface (`contract/body_commands.md`) inside the live patrol process.

One process owns the dog. Idle, it explores and greets; when the family app sends a
mission through `go2_errand.py`, the errand POSTs body commands here and the patrol
loop executes them with the same guardrails, then goes back to exploring. Receipts
have the body service's shape (`accepted -> executing -> completed | failed | cancelled`)
so the errand's `BodyClient` works unchanged against either process.

Thread-safe: the HTTP handler thread submits and reads, the asyncio control loop
starts and finishes. Validation is the body's (`go2_body.validate_command`).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

TERMINAL = ("completed", "failed", "cancelled")
MAX_RECEIPTS = 200
MAX_WAITING = 8  # overlapping missions queue up to this many; beyond that the body answers 409 busy


class MissionBoard:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.receipts: OrderedDict[str, dict] = OrderedDict()
        self.current: dict | None = None
        self.parent: dict | None = None   # an "instruct" receipt whose planned steps run as child receipts
        self.queue: list[dict] = []
        self.pending_stops: list[dict] = []
        self.stop_requested = False
        self.waiting: list[dict] = []     # missions accepted while another runs; started in order (state "queued")

    def submit(self, payload) -> tuple[int, dict]:
        """Body-compatible: 202 accepted, 200 on a repeated command_id, 409 busy, 400 invalid."""
        from robot.dog.runtime.body import validate_command  # lazy: go2_body imports patrol helpers, avoid the import cycle
        try:
            command_id, name, args = validate_command(payload)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        with self.lock:
            if command_id in self.receipts:
                return 200, dict(self.receipts[command_id])
            if name == "stop":
                receipt = self._new(command_id, name, args)
                if self.current is not None and self.current["state"] in ("accepted", "executing"):
                    self.current["state"], self.current["error"] = "cancelled", "stopped by operator"
                    self.current["finished_at_ms"] = int(self.clock() * 1000)
                if self.parent is not None and self.parent["state"] not in TERMINAL:
                    self.parent["state"], self.parent["error"] = "cancelled", "stopped by operator"
                    self.parent["finished_at_ms"] = int(self.clock() * 1000)
                for w in self.waiting:
                    w["state"], w["error"], w["finished_at_ms"] = "cancelled", "stopped by operator", int(self.clock() * 1000)
                self.current, self.parent, self.queue, self.waiting = None, None, [], []
                receipt.update(stop_requested=True, stop_code=None, processed_at_ms=None, ack_ms=None)
                self._remember(receipt)
                self.pending_stops.append(receipt)
                self.stop_requested = True
                return 202, dict(receipt)
            if (self.current is not None and self.current["state"] in ("accepted", "executing")) or self.parent is not None:
                if len(self.waiting) >= MAX_WAITING:
                    return 409, {"error": "busy", "current": dict(self.current or self.parent), "waiting": len(self.waiting)}
                receipt = self._new(command_id, name, args)  # overlapping requests wait their turn instead of failing
                receipt["state"], receipt["position"] = "queued", len(self.waiting) + 1
                receipt["behind"] = (self.current or self.parent)["command_id"]
                self._remember(receipt)
                self.waiting.append(receipt)
                return 202, dict(receipt)
            receipt = self._new(command_id, name, args)
            self._remember(receipt)
            self.current = receipt
            return 202, dict(receipt)

    def _new(self, command_id, name, args):
        return {"command_id": command_id, "name": name, "args": args, "state": "accepted",
                "accepted_at_ms": int(self.clock() * 1000), "result": None, "error": None}

    def _remember(self, receipt):
        self.receipts[receipt["command_id"]] = receipt
        while len(self.receipts) > MAX_RECEIPTS:
            self.receipts.popitem(last=False)

    def recent(self, n=12) -> list[dict]:
        """The last `n` receipts, newest first (copies), for the operator's page."""
        with self.lock:
            return [dict(r) for r in list(self.receipts.values())[-max(0, int(n)):][::-1]]

    def get(self, command_id) -> dict | None:
        with self.lock:
            r = self.receipts.get(command_id)
            return dict(r) if r else None

    # -- control-loop side ---------------------------------------------------------------------------------------
    def take(self) -> dict | None:
        """The accepted mission to start now (marks it executing), else None. After an instruct's plan was
        chained, its steps come out here one at a time as child receipts."""
        with self.lock:
            if self.stop_requested:
                return None
            cur = self.current
            if cur is not None and cur["state"] == "accepted":
                cur["state"], cur["started_at_ms"] = "executing", int(self.clock() * 1000)
                return cur
            if cur is None and self.parent is None and self.waiting:  # next in line
                nxt = self.waiting.pop(0)
                if nxt["state"] == "queued":
                    nxt["state"], nxt["started_at_ms"] = "executing", int(self.clock() * 1000)
                    nxt.pop("position", None)
                    self.current = nxt
                    return nxt
            if cur is None and self.parent is not None and self.queue:
                step = self.queue.pop(0)
                n = self.parent["progress"]["done"] + 1
                child = self._new(f"{self.parent['command_id']}#{n}", step["name"], step["args"])
                child["parent"], child["state"], child["started_at_ms"] = self.parent["command_id"], "executing", int(self.clock() * 1000)
                self._remember(child)
                self.parent["progress"]["step"] = f"{n}/{self.parent['progress']['steps']} {step['name']}"
                self.current = child
                return child
            return None

    def chain(self, parent: dict, steps: list[dict], *, reply: str = "", source: str = "") -> None:
        """Run `steps` (validated {"name","args"}) one after another under the executing `parent`; the parent
        completes when the last step does, fails on the first failed step."""
        with self.lock:
            if parent["state"] != "executing":
                return
            parent["progress"] = {"steps": len(steps), "done": 0, "reply": reply, "source": source, "results": []}
            self.parent, self.queue = parent, [dict(s) for s in steps]
            if self.current is parent:
                self.current = None
            if not steps:
                self._finish_parent(error=reply or "No executable steps were found for this instruction.")

    def executing(self) -> dict | None:
        with self.lock:
            cur = self.current
            return cur if cur is not None and cur["state"] == "executing" else None

    def progress(self, receipt, **fields):
        with self.lock:
            receipt.setdefault("progress", {}).update(fields)

    def finish(self, receipt, *, result=None, error=None):
        with self.lock:
            if receipt["state"] in TERMINAL:
                return
            receipt["state"] = "failed" if error else "completed"
            receipt["result"], receipt["error"] = result, error
            receipt["finished_at_ms"] = int(self.clock() * 1000)
            if self.current is receipt:
                self.current = None
            parent = self.parent
            if parent is not None and receipt.get("parent") == parent["command_id"]:
                parent["progress"]["done"] += 1
                parent["progress"]["results"].append({"name": receipt["name"], "state": receipt["state"], "result": result, "error": error})
                if error:
                    self.queue = []
                    self._finish_parent(error=f"step {receipt['name']} failed: {error}")
                elif not self.queue:
                    self._finish_parent(result={"reply": parent["progress"].get("reply"), "steps": parent["progress"]["done"],
                                                "source": parent["progress"].get("source"), "results": parent["progress"]["results"]})

    def _finish_parent(self, *, result=None, error=None):
        parent = self.parent
        if parent is not None and parent["state"] not in TERMINAL:
            parent["state"] = "failed" if error else "completed"
            parent["result"], parent["error"] = result, error
            parent["finished_at_ms"] = int(self.clock() * 1000)
        self.parent, self.queue = None, []

    def take_stops(self) -> list[dict]:
        """Snapshot only requests covered by the next physical StopMove request."""
        with self.lock:
            pending, self.pending_stops = self.pending_stops, []
            self.stop_requested = False
            for receipt in pending:
                receipt["state"] = "executing"
                receipt["started_at_ms"] = int(self.clock() * 1000)
            return pending

    def finish_stops(self, pending, *, code, ack_ms, error=None):
        """Record software acknowledgment; never claim the robot physically stopped."""
        with self.lock:
            processed = int(self.clock() * 1000)
            for receipt in pending:
                if receipt["state"] in TERMINAL:
                    continue
                failure = error or (None if type(code) is int and code == 0 else "StopMove acknowledgment missing" if code is None else f"StopMove rejected: {code}")
                result = {"stop_code": code, "processed_at_ms": processed, "ack_ms": ack_ms,
                          "note": "Software StopMove acknowledgment only; not proof the robot physically stopped."}
                receipt.update(result)
                receipt.update(state="failed" if failure else "completed", result=result,
                               error=failure, finished_at_ms=processed)
