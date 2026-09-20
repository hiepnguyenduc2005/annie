#!/usr/bin/env python3
"""Family-message errand relay for the physical Go2: find Jeanine, say the message, listen, report.

Robot-side adapter for `contract/family_messages.md`. app_backend POSTs a family
message to `/dispatch`; this service acknowledges at once, then runs one errand
at a time (FIFO queue) and reports every step to app_backend's
`POST /internal/events`. The dog is a hand and an arm: this process never
touches hardware or WebRTC. It sends succinct commands (`find_person`, `say`,
`listen`, `stop`) to the separate body service (`robot/go2_body.py`) over HTTP
and watches the receipts.

What this is, plainly:
- A software relay. The spoken line is a fixed template around the family
  member's own words; no model phrases or interprets anything.
- Playback is the host computer's speaker and listening is the host microphone,
  both through the body service. It is not the robot's own audio.
- The reply is classified by exact-phrase match (`audio_reply.classify`: "okay",
  "help me", ...). That is string matching, not understanding; anything else is
  passed on verbatim as unclear. "May need help" prompts the family to check in;
  it is not an assessment of Jeanine's condition.
- "Found Jeanine" is the body's face-match estimate, or an unnamed person assumed
  to be her. Someone the body names as another person is never given the message.
- `stop` is a software stop request to the body service. It is not a hardware
  emergency stop and cannot stop the robot when the body service or its link is
  down. Supervised use only, operator nearby.

Run from the repository root. ANNIE_INTERNAL_SECRET must match the app_backend
host, whose ROBOT_BACKEND_URL points at this service (port 8010, not the body):
  ANNIE_INTERNAL_SECRET=... .venv/bin/python robot/go2_errand.py --port 8010
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.app_backend.app.audio_reply import classify  # noqa: E402

SCRIPT_VERSION = "go2-errand/0.1"
RESIDENT = "Jeanine"
SAY_MAX_CHARS, TEXT_MAX_CHARS, NAME_MAX_CHARS, TRANSCRIPT_MAX_CHARS = 300, 2000, 80, 500
FIND_TIMEOUT_S, LISTEN_MAX_S = 90, 10
DEADLINES_S = {"find_person": 120.0, "say": 45.0, "listen": 25.0}  # hard client-side limits; anything else 15 s
BUSY_WAIT_S = 5.0  # how long a 409 busy from the body is retried before giving up
TERMINAL_KINDS = ("completed", "failed")
RECEIPT_TERMINAL = ("completed", "failed", "cancelled")
EVENT_RETRIES = {"progress": 1, "terminal": 3}
MAX_BODY_BYTES, MAX_OPEN_RUNS, MAX_RUNS = 65536, 10, 200


class ErrandError(Exception):
    """A failure whose message is safe to show the family: no vendor text, stack traces, or secrets."""


def _log(text: str) -> None:
    print(f"go2-errand: {text}", file=sys.stderr, flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def phrase_message(author_name: str, text: str) -> str:
    """The exact line Annie says. Deterministic; the quote is cut so the whole line fits one `say`."""
    prefix = f"{RESIDENT}, it's Annie. {author_name.strip()} asked me to pass this along: \""
    quote = " ".join(text.split())
    room = SAY_MAX_CHARS - len(prefix) - 1
    if len(quote) > room:
        quote = quote[:room - 1].rstrip() + "…"
    return f'{prefix}{quote}"'


def interpret_reply(transcript: str | None) -> dict:
    """Map what was heard to the terminal payload. Exact-phrase match only; not understanding."""
    heard = (transcript or "").strip()
    if not heard:
        return {"reply": "none", "mood": "neutral", "detail": "Message delivered; no reply heard."}
    intent = classify(heard)
    if intent == "reassurance":
        return {"reply": "okay", "mood": "happy", "detail": f"{RESIDENT} says okay. 🙂"}
    if intent == "concern":
        return {"reply": "concern", "mood": "worried", "detail": f'{RESIDENT} may need help: "{heard}"'}
    return {"reply": "unclear", "mood": "neutral", "detail": f'{RESIDENT} said: "{heard}"'}


def parse_dispatch(raw: bytes) -> dict:
    """Strictly validate a /dispatch body; ValueError carries a safe message. Unknown keys are ignored."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    try:
        run_id = str(uuid.UUID(data["run_id"])) if isinstance(data.get("run_id"), str) else None
    except ValueError:
        run_id = None
    if run_id is None:
        raise ValueError("run_id must be a UUID string")
    text, author_name = data.get("text"), data.get("author_name")
    if not isinstance(text, str) or not 1 <= len(text) <= TEXT_MAX_CHARS:
        raise ValueError(f"text must be a string of 1..{TEXT_MAX_CHARS} characters")
    if not isinstance(author_name, str) or not 1 <= len(author_name.strip()) or len(author_name) > NAME_MAX_CHARS:
        raise ValueError(f"author_name must be a string of 1..{NAME_MAX_CHARS} characters")
    author_id, dispatched_at = data.get("author_id"), data.get("dispatched_at")
    if author_id is not None and (not isinstance(author_id, str) or not 1 <= len(author_id) <= NAME_MAX_CHARS):
        raise ValueError("author_id must be a short string")
    if dispatched_at is not None and (type(dispatched_at) is not int or dispatched_at < 0):
        raise ValueError("dispatched_at must be epoch milliseconds")
    return {"run_id": run_id, "author_id": author_id, "author_name": author_name.strip(), "text": text,
            "dispatched_at": dispatched_at}


class Errand:
    """One family-message errand, decided off-robot: find, say, listen, report. No HTTP, no hardware.

    `body_command(name, args) -> result dict` runs one body command to completion and raises on failure.
    `post_event(run_id, kind, payload, at) -> bool` stores one app_backend event (one attempt; retries
    live here). Events leave through an ordered outbox so a slow app_backend never delays the physical
    sequence: listening has to start the moment the line has played. One run at a time per instance.
    """

    def __init__(self, body_command, post_event, *, status=_log, clock_ms=now_ms, retry_pause_s=0.5):
        self.body_command, self.post_event = body_command, post_event
        self.status, self.clock_ms, self.retry_pause_s = status, clock_ms, retry_pause_s

    async def run(self, run_id: str, author_name: str, text: str, *, log: list | None = None) -> str:
        """Drive one errand to exactly one terminal event; returns 'completed' or 'failed'."""
        self.run_id, self.tag, self.closed = run_id, run_id[:8], False
        self.log = [] if log is None else log
        self.outbox: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(self._pump())
        try:
            return await self._sequence(author_name, text)
        except asyncio.CancelledError:
            await self._fail("Annie's errand service stopped before the message was delivered.")
            raise
        except Exception as exc:
            safe = str(exc) if isinstance(exc, ErrandError) else f"Annie hit an unexpected problem ({type(exc).__name__})."
            return await self._fail(safe)
        finally:
            self.outbox.put_nowait(None)
            await pump

    async def _sequence(self, author_name: str, text: str) -> str:
        self._event("navigating", {"detail": f"Looking for {RESIDENT}."})
        found = await self.body_command("find_person", {"name": RESIDENT, "timeout_s": FIND_TIMEOUT_S, "approach": True})
        name = str((found.get("identity") or {}).get("name") or "").strip()
        if not found.get("found") or (name and name.casefold() != RESIDENT.casefold()):
            return await self._fail(f"Could not find {RESIDENT}.")  # a differently named person is not her
        self._event("arrived", {"detail": f"Found {RESIDENT}." if name else f"Found someone; assuming it is {RESIDENT}."})
        line = phrase_message(author_name, text)
        if not (await self.body_command("say", {"text": line})).get("played"):
            raise ErrandError("Could not play the message on the speaker.")
        self._event("speaking", {"text": line})  # only after playback really finished
        self._event("listening", {})
        reply = await self.body_command("listen", {"max_s": LISTEN_MAX_S})
        transcript = reply.get("transcript") if reply.get("heard") else None
        transcript = transcript.strip()[:TRANSCRIPT_MAX_CHARS] if isinstance(transcript, str) else ""
        if transcript:
            self._event("heard", {"transcript": transcript})
        outcome = interpret_reply(transcript)
        self.status(f"run {self.tag} reply={outcome['reply']} ({len(transcript)} chars heard)")
        self._event("completed", {"detail": outcome["detail"], "reply": outcome["reply"], "mood": outcome["mood"]})
        return "completed"

    async def _fail(self, error: str) -> str:
        """Queue the single `failed` event and send the software stop at once; neither waits on the other."""
        self._event("failed", {"error": error})
        try:
            await self.body_command("stop", {})
            self.status(f"run {self.tag} software stop sent")
        except Exception as exc:
            self.status(f"run {self.tag} software stop NOT confirmed ({type(exc).__name__})")
        return "failed"

    def _event(self, kind: str, payload: dict) -> None:
        if self.closed:  # the contract answers 409 to anything after a terminal event
            return
        self.closed = kind in TERMINAL_KINDS
        entry = {"kind": kind, "payload": payload, "at": self.clock_ms(), "delivered": None}
        self.log.append(entry)
        self.outbox.put_nowait(entry)
        self.status(f"run {self.tag} {kind}")

    async def _pump(self) -> None:
        while (entry := await self.outbox.get()) is not None:
            entry["delivered"] = await self._deliver(entry)

    async def _deliver(self, entry: dict) -> bool:
        kind = entry["kind"]
        attempts = 1 + EVENT_RETRIES["terminal" if kind in TERMINAL_KINDS else "progress"]
        for attempt in range(1, attempts + 1):
            try:
                if await self.post_event(self.run_id, kind, entry["payload"], entry["at"]):
                    return True
                note = "not accepted"
            except ErrandError as exc:  # rejected for good (closed/unknown run, bad secret): a retry cannot help
                self.status(f"run {self.tag} {kind} event rejected: {exc}")
                return False
            except Exception as exc:
                note = type(exc).__name__
            self.status(f"run {self.tag} {kind} event attempt {attempt}/{attempts} failed ({note})")
            if attempt < attempts:
                await asyncio.sleep(self.retry_pause_s)
        return False


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ErrandError("The robot body sent an unreadable reply.")
    return data


class BodyClient:
    """Thin HTTP adapter for the body service: submit a command, poll its receipt to a terminal state."""

    def __init__(self, base_url: str, *, token: str | None = None, poll_s: float = 0.5, deadlines: dict | None = None,
                 transport=None, status=_log):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5.0, trust_env=False, transport=transport,
                                        headers={"X-Body-Token": token} if token else None)
        self.poll_s, self.deadlines, self.status = poll_s, {**DEADLINES_S, **(deadlines or {})}, status

    async def command(self, name: str, args: dict | None = None) -> dict:
        """Run one body command to completion and return its result dict; ErrandError otherwise."""
        if name == "stop":  # the dedicated endpoint pre-empts an executing command; POST /command would answer busy
            return await self.stop()
        deadline_s = self.deadlines.get(name, 15.0)
        try:
            receipt = await asyncio.wait_for(self._drive(str(uuid.uuid4()), name, args or {}), deadline_s)
        except asyncio.TimeoutError:
            raise ErrandError(f"The robot body did not finish {name} within {deadline_s:g} s.") from None
        if receipt.get("state") != "completed":
            self.status(f"body {name} {receipt.get('state')}: {str(receipt.get('error') or '')[:120]}")
            raise ErrandError(f"The robot body could not finish {name} ({receipt.get('state')}).")
        result = receipt.get("result")
        return result if isinstance(result, dict) else {}

    async def stop(self) -> dict:
        """Software stop request. Not a hardware emergency stop."""
        response = await self._request("POST", "/stop")
        if not response.is_success:
            raise ErrandError(f"The robot body refused the software stop (HTTP {response.status_code}).")
        return _json(response)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _drive(self, command_id: str, name: str, args: dict) -> dict:
        body, busy_until = {"command_id": command_id, "name": name, "args": args}, time.monotonic() + BUSY_WAIT_S
        while (response := await self._request("POST", "/command", json=body)).status_code == 409:
            if time.monotonic() >= busy_until:
                raise ErrandError("The robot body is busy with another command.")
            await asyncio.sleep(self.poll_s)
        while True:
            if not response.is_success:
                raise ErrandError(f"The robot body rejected {name} (HTTP {response.status_code}).")
            receipt = _json(response)
            if receipt.get("state") in RECEIPT_TERMINAL:
                return receipt
            await asyncio.sleep(self.poll_s)
            response = await self._request("GET", f"/command/{command_id}")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Up to three tries on transport errors; safe because command_id makes the POST idempotent."""
        for attempt in range(3):
            try:
                return await self.client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 2:
                    raise ErrandError("The robot body service is unreachable.") from None
                await asyncio.sleep(self.poll_s)


class AppClient:
    """Thin HTTP adapter for app_backend's POST /internal/events: one attempt, 3 s; the Errand owns retries."""

    def __init__(self, base_url: str, secret: str, *, transport=None):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=3.0, trust_env=False, transport=transport,
                                        headers={"X-Internal-Secret": secret})

    async def post_event(self, run_id: str, kind: str, payload: dict, at: int) -> bool:
        response = await self.client.post("/internal/events", json={"run_id": run_id, "kind": kind,
                                                                    "payload": payload, "at": at})
        if response.status_code in (400, 401, 403, 404, 409, 422):
            raise ErrandError(f"app_backend answered HTTP {response.status_code}")
        return response.is_success

    async def aclose(self) -> None:
        await self.client.aclose()


class ErrandService:
    """Run registry and FIFO queue: one errand at a time on an asyncio loop in a background thread.

    `run_errand(run) -> 'completed' | 'failed'` is an async callable; `run` carries the validated dispatch
    plus `state` and the `events` list the errand appends to. `handle()` is the whole HTTP surface without
    sockets, so it can be tested directly.
    """

    def __init__(self, run_errand, *, body_url: str = "", status=_log):
        self.run_errand, self.body_url, self.status = run_errand, body_url, status
        self.runs: dict[str, dict] = {}
        self.open = 0  # accepted + queued + running
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.thread = threading.Thread(target=self._serve, name="go2-errand-loop", daemon=True)

    def start(self) -> "ErrandService":
        self.thread.start()
        return self

    def close(self, *closers, timeout_s: float = 10.0) -> None:
        """Cancel the active errand (it posts `failed` and sends the software stop), then stop the loop."""
        if self.thread.is_alive():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(closers), self.loop)
            with contextlib.suppress(Exception):
                future.result(timeout_s)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(2.0)
        if not self.loop.is_running() and not self.loop.is_closed():
            self.loop.close()  # never started, or the loop thread has already returned

    def handle(self, method: str, path: str, raw: bytes = b"") -> tuple[int, dict]:
        path = path.split("?", 1)[0].rstrip("/")
        if method == "GET" and path == "/health":
            return 200, {"ok": True, "body_url": self.body_url, "busy": self.open > 0}
        if method == "GET" and path.startswith("/runs/"):
            with contextlib.suppress(ValueError), self.lock:
                run = self.runs.get(str(uuid.UUID(path[len("/runs/"):])))
                if run is not None:
                    return 200, {"run_id": run["run_id"], "state": run["state"], "events": [dict(e) for e in run["events"]]}
            return 404, {"error": "unknown run"}
        if method == "POST" and path == "/dispatch":
            try:
                return self.dispatch(parse_dispatch(raw))
            except ValueError as exc:
                return 400, {"error": str(exc)}
        return 404, {"error": "not found"}

    def dispatch(self, message: dict) -> tuple[int, dict]:
        """202 for a new run (accepted, or queued behind another); 200 with the current state for a repeat."""
        with self.lock:
            run = self.runs.get(message["run_id"])
            if run is not None:
                return 200, {"run_id": run["run_id"], "state": run["state"]}
            if self.open >= MAX_OPEN_RUNS:
                return 503, {"error": "errand queue is full"}
            run = {**message, "state": "queued" if self.open else "accepted", "events": []}
            self.runs[run["run_id"]] = run
            self.open += 1
            for stale in [k for k, r in self.runs.items() if r["state"] in TERMINAL_KINDS][:max(0, len(self.runs) - MAX_RUNS)]:
                del self.runs[stale]
            self.loop.call_soon_threadsafe(self.queue.put_nowait, run["run_id"])  # inside the lock: strict FIFO
        self.status(f"run {run['run_id'][:8]} {run['state']} from {run['author_name']} ({len(run['text'])} chars)")
        return 202, {"run_id": run["run_id"], "state": run["state"]}

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.worker = self.loop.create_task(self._work())
        self.loop.run_forever()

    async def _work(self) -> None:
        while True:
            run = self.runs[await self.queue.get()]
            run["state"], state = "running", "failed"
            try:
                state = await self.run_errand(run)
            except Exception as exc:
                self.status(f"run {run['run_id'][:8]} errand runner crashed ({type(exc).__name__})")
            finally:
                with self.lock:
                    run["state"] = state if state in TERMINAL_KINDS else "failed"
                    self.open -= 1
                self.status(f"run {run['run_id'][:8]} finished: {run['state']}")

    async def _shutdown(self, closers) -> None:
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.worker
        for closer in closers:
            with contextlib.suppress(Exception):
                await closer()


def make_handler(service: ErrandService):
    class Handler(BaseHTTPRequestHandler):
        server_version = SCRIPT_VERSION
        timeout = 10  # seconds on the socket, so a stalled client cannot pin a thread

        def log_message(self, *args):  # the service logs one line per step; no access log
            pass

        def _respond(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length > MAX_BODY_BYTES:
                code, payload = 413, {"error": "body too large"}
            elif length < 0:
                code, payload = 400, {"error": "bad Content-Length"}
            else:
                try:
                    code, payload = service.handle(method, self.path, self.rfile.read(length) if length else b"")
                except Exception as exc:
                    _log(f"request failed ({type(exc).__name__})")
                    code, payload = 500, {"error": "internal error"}
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._respond("GET")

        def do_POST(self):
            self._respond("POST")

    return Handler


def _interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Family-message errand relay for the physical Go2 (software relay).")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--body-url", default=os.environ.get("ANNIE_BODY_URL") or "http://127.0.0.1:8001")
    parser.add_argument("--app-url", default=os.environ.get("ANNIE_APP_URL") or "http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    for url in (args.body_url, args.app_url):
        if not url.startswith(("http://", "https://")):
            parser.error(f"not an http(s) URL: {url}")
    secret = (os.environ.get("ANNIE_INTERNAL_SECRET") or "").strip()
    if not secret:
        _log("ANNIE_INTERNAL_SECRET is not set. app_backend rejects every event without it; "
             "export the same value the app_backend host uses, then start again.")
        return 2
    body = BodyClient(args.body_url, token=(os.environ.get("ANNIE_BODY_TOKEN") or "").strip() or None)
    app = AppClient(args.app_url, secret)

    async def run_errand(run: dict) -> str:
        return await Errand(body.command, app.post_event).run(run["run_id"], run["author_name"], run["text"],
                                                               log=run["events"])

    service = ErrandService(run_errand, body_url=args.body_url).start()
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    except OSError as exc:
        _log(f"cannot listen on {args.host}:{args.port} ({type(exc).__name__})")
        service.close(body.aclose, app.aclose)
        return 1
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, _interrupt)
    _log(f"{SCRIPT_VERSION} listening on {args.host}:{args.port}; body {args.body_url}; app {args.app_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("interrupted; stopping any active errand (software stop)")
    finally:
        server.server_close()
        service.close(body.aclose, app.aclose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
