#!/usr/bin/env python3
"""E2E probe for the offline simulated stack (launched by robot/demo_sim.py).

SAFETY GATE: nothing is ever POSTed unless dog telemetry, app dog-status and
dog /voice all report the simulation/mock state. On any mismatch the probe
fails immediately with exit code 1 and sends nothing.

Checks, in order:
  1. health endpoints (app, errand, dog)
  2. simulation gate: source=simulation + connected (telemetry and app status),
     /voice reports mocked simulation audio
  3. a say mission via POST /command completes with source=simulation,
     where="simulation mock"
  4. a family relay message (tell Grandma ...) through POST /api/messages
     completes with navigating/arrived/speaking/listening/heard/completed
     events and the mocked transcript "okay thank you"

Usage: .venv/bin/python robot/verify_sim.py [--app-port 8120 --errand-port 8110 --dog-port 8111]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

PASS, FAIL, SKIP = [], [], []

EXPECTED_RELAY_EVENTS = ("navigating", "arrived", "speaking", "listening", "heard", "completed")
MOCK_TRANSCRIPT = "okay thank you"


def ok(label, detail=""):
    PASS.append(label)
    print(f"  [PASS] {label}" + (f" ({detail})" if detail else ""))


def bad(label, detail=""):
    FAIL.append(label)
    print(f"  [FAIL] {label}" + (f" ({detail})" if detail else ""))


def skp(label, detail=""):
    SKIP.append(label)
    print(f"  [SKIP] {label}" + (f" ({detail})" if detail else ""))


def req(url, method="GET", body=None, headers=None, timeout=8.0):
    """Returns (status, parsed-json-or-raw-text). Never raises on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def poll(check, timeout_s, interval=1.0):
    """Bounded wait; returns the first truthy result or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(interval)
    return None


def simulation_gate(tel, app_status, voice):
    """True only for a live simulated stack. Callers must refuse every
    state-changing request when this returns False."""
    reasons = []
    if not isinstance(tel, dict) or tel.get("source") != "simulation":
        reasons.append("telemetry source is not simulation")
    if not isinstance(tel, dict) or tel.get("connected") is not True:
        reasons.append("telemetry connected is not true")
    if not isinstance(app_status, dict) or app_status.get("source") != "simulation":
        reasons.append("app dog-status source is not simulation")
    if not isinstance(app_status, dict) or app_status.get("available") is not True:
        reasons.append("app dog-status available is not true")
    if not isinstance(app_status, dict) or app_status.get("connected") is not True:
        reasons.append("app dog-status connected is not true")
    if not isinstance(voice, dict) or voice.get("source") != "simulation" or voice.get("mocked") is not True:
        reasons.append("voice is not mocked simulation audio")
    return (not reasons), reasons


def main(argv=None) -> int:
    global PASS, FAIL, SKIP
    PASS, FAIL, SKIP = [], [], []  # allow repeat in-process runs (tests)
    p = argparse.ArgumentParser(description="E2E probe for the offline simulated stack")
    p.add_argument("--app-port", type=int, default=8120)
    p.add_argument("--errand-port", type=int, default=8110)
    p.add_argument("--dog-port", type=int, default=8111)
    p.add_argument("--wait-s", type=float, default=90.0)
    args = p.parse_args(argv)

    app = f"http://127.0.0.1:{args.app_port}"
    errand = f"http://127.0.0.1:{args.errand_port}"
    dog = f"http://127.0.0.1:{args.dog_port}"

    print("== services up")
    st, _ = req(app + "/health")
    (ok if st == 200 else bad)(f"app /health {app}", f"HTTP {st}")
    st, body = req(errand + "/health")
    (ok if (st == 200 and isinstance(body, dict) and body.get("ok") is True) else bad)(
        f"errand /health {errand}", f"HTTP {st}")
    st, body = req(dog + "/health")
    (ok if (st == 200 and isinstance(body, dict) and body.get("ok") is True) else bad)(
        f"dog /health {dog}", f"HTTP {st}")

    print("== simulation gate (no command is sent unless this passes)")
    _, tel = req(dog + "/telemetry.json")
    _, app_status = req(app + "/api/dog/status")
    _, voice = req(dog + "/voice")
    gate_ok, reasons = simulation_gate(tel, app_status, voice)
    if gate_ok:
        ok("simulation gate: source=simulation, connected, mocked voice")
    else:
        for reason in reasons:
            bad("simulation gate", reason)
        print("REFUSING to send any command: this is not a verified simulated stack.")
        print(f"== summary: {len(PASS)} pass, {len(FAIL)} fail, {len(SKIP)} skip")
        return 1

    print("== say mission through dog /command (mock audio receipt)")
    say_id = "probe-say-" + uuid.uuid4().hex[:12]
    st, receipt = req(dog + "/command", method="POST",
                      body={"command_id": say_id, "name": "say", "args": {"text": "Probe check, all well."}})
    accepted = st == 202 and isinstance(receipt, dict) and receipt.get("state") in ("accepted", "executing", "queued")
    (ok if accepted else bad)("POST /command say accepted", f"HTTP {st}")

    def say_done():
        st2, r2 = req(dog + "/command/" + say_id)
        if st2 == 200 and isinstance(r2, dict) and r2.get("state") in ("completed", "failed", "cancelled"):
            return r2
        return None

    receipt = poll(say_done, args.wait_s)
    if receipt and receipt.get("state") == "completed":
        result = receipt.get("result") or {}
        (ok if result.get("source") == "simulation" else bad)("say receipt source=simulation", str(result.get("source")))
        (ok if result.get("where") == "simulation mock" else bad)("say receipt where='simulation mock'", str(result.get("where")))
        (ok if result.get("played") is True else bad)("say receipt played=true")
    elif receipt:
        bad("say mission terminal state", f"state={receipt.get('state')} error={receipt.get('error')}")
    else:
        bad("say mission finished", f"no terminal receipt within {args.wait_s:g}s")

    print("== family relay mission through app -> errand -> dog")
    st, posted = req(app + "/api/messages", method="POST",
                     body={"author_id": "zach", "text": "tell Grandma the simulator check is complete"})
    if st != 202 or not isinstance(posted, dict) or "run_id" not in posted:
        bad("POST /api/messages accepted", f"HTTP {st} body={posted}")
    else:
        run_id = posted["run_id"]
        ok("POST /api/messages accepted", f"run {run_id}")

        def run_state():
            st2, run = req(app + f"/api/runs/{run_id}")
            if st2 == 200 and isinstance(run, dict) and run.get("status") in ("completed", "failed"):
                return run
            return None

        run = poll(run_state, max(args.wait_s, 120.0))
        if run is None:
            bad("family mission reached terminal state", "no terminal run within the wait window")
        elif run.get("status") == "completed":
            ok("family mission completed", f"outcome={run.get('outcome')}")
        else:
            ev = [e.get("kind") for e in (run.get("events") or [])]
            bad("family mission completed", f"status={run.get('status')} events={ev}")

        kinds = [e.get("kind") for e in ((run or {}).get("events") or [])]
        for expected in EXPECTED_RELAY_EVENTS:
            (ok if expected in kinds else bad)(f"run event '{expected}' present", str(kinds))

        # The transcript that matters is the one the RUN heard (the errand's
        # 'heard' event payload), not an unrelated autonomous conversation.
        heard_payloads = [((e.get("payload") or {}).get("transcript") or "")
                          for e in ((run or {}).get("events") or []) if e.get("kind") == "heard"]
        (ok if any(h.strip().lower() == MOCK_TRANSCRIPT for h in heard_payloads) else bad)(
            f"run 'heard' event transcript == '{MOCK_TRANSCRIPT}'", str(heard_payloads))

        # Conversations are an independent, optional observation: reported when
        # present, never load-bearing (start-paused scenes may produce none).
        _, tel_after = req(dog + "/telemetry.json")
        convos = (tel_after or {}).get("conversations") if isinstance(tel_after, dict) else []
        heard = [c.get("heard") for c in (convos or []) if isinstance(c, dict)]
        (ok if any((h or "").strip().lower() == MOCK_TRANSCRIPT for h in heard) else skp)(
            f"mocked transcript '{MOCK_TRANSCRIPT}' in conversations", str(heard))
        conv_src = [c.get("source") for c in (convos or []) if isinstance(c, dict)]
        (ok if conv_src and all(s == "simulation" for s in conv_src) else skp)(
            "conversations source=simulation", str(conv_src))

    print(f"== summary: {len(PASS)} pass, {len(FAIL)} fail, {len(SKIP)} skip")
    for f in FAIL:
        print(f"  FAIL {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
