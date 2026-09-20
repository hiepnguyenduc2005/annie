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


class MissionBoard:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.receipts: OrderedDict[str, dict] = OrderedDict()
        self.current: dict | None = None

    def submit(self, payload) -> tuple[int, dict]:
        """Body-compatible: 202 accepted, 200 on a repeated command_id, 409 busy, 400 invalid."""
        from go2_body import validate_command  # lazy: go2_body imports patrol helpers, avoid the import cycle
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
                self.current = None
                receipt["state"], receipt["result"] = "completed", {"stop_code": None, "note": "cancelled the mission; the patrol loop sends StopMove"}
                receipt["finished_at_ms"] = int(self.clock() * 1000)
                receipt["stop_requested"] = True
                self._remember(receipt)
                self.stop_requested = True
                return 200, dict(receipt)
            if self.current is not None and self.current["state"] in ("accepted", "executing"):
                return 409, {"error": "busy", "current": dict(self.current)}
            receipt = self._new(command_id, name, args)
            self._remember(receipt)
            self.current = receipt
            return 202, dict(receipt)

    stop_requested = False

    def _new(self, command_id, name, args):
        return {"command_id": command_id, "name": name, "args": args, "state": "accepted",
                "accepted_at_ms": int(self.clock() * 1000), "result": None, "error": None}

    def _remember(self, receipt):
        self.receipts[receipt["command_id"]] = receipt
        while len(self.receipts) > MAX_RECEIPTS:
            self.receipts.popitem(last=False)

    def get(self, command_id) -> dict | None:
        with self.lock:
            r = self.receipts.get(command_id)
            return dict(r) if r else None

    # -- control-loop side ---------------------------------------------------------------------------------------
    def take(self) -> dict | None:
        """The accepted mission to start now (marks it executing), else None."""
        with self.lock:
            cur = self.current
            if cur is not None and cur["state"] == "accepted":
                cur["state"], cur["started_at_ms"] = "executing", int(self.clock() * 1000)
                return cur
            return None

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

    def consume_stop(self) -> bool:
        with self.lock:
            flag, self.stop_requested = self.stop_requested, False
            return flag
