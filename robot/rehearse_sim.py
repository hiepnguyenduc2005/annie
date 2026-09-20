#!/usr/bin/env python3
"""Bounded synthetic E2E rehearsal against the offline simulated stack.

Drives the real HTTP surfaces launched by robot/demo_sim.py: top-level
app_backend family API on 8120, errand 8110, simulated dog 8111. No hardware,
no providers, no paid calls.

SAFETY GATE: nothing is POSTed unless robot/verify_sim.simulation_gate passes
(telemetry and app dog-status source=simulation and connected, dog /voice
reports mocked simulation audio). Any mismatch fails closed with exit 1.

What it checks (any failure exits 1):
  1. N successful deliveries (default 5), each a fresh run (dedup = failure),
     author zach, known text "tell Grandma to charge your phone" with a unique
     numeric suffix; run must reach completed with navigating/arrived/speaking/
     listening/heard/completed in order and the heard transcript equal to the
     mock reply ("okay thank you").
  2. Pause drill: identical repeat while executing returns the SAME run_id;
     a second distinct request queues behind it; POST /api/family/pause
     cancels both, requires stop_confirmed=true; both stay cancelled with no
     automatic replay; one fresh explicit request then completes.
  3. Telemetry: direct pose/track/fps samples and spacetime frame counts are
     reported separately; any displacement figure is labelled inferred.

Summary JSON (default .data/sim/rehearse-<ts>.json) records config, gate,
per-run ids/events/elapsed/failures, pause results, telemetry, and the stated
source/model/audio facts.

Usage: .venv/bin/python robot/rehearse_sim.py [--app-port 8120 --errand-port
       8110 --dog-port 8111 --deliveries 5 --run-timeout 180]
  Optional: --api-token TOKEN  (family bearer token; loopback demo uses none)
  Optional: --mock-transcript TEXT overrides the expected reply
  Optional: --summary-out PATH
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot.verify_sim import simulation_gate  # noqa: E402

APP_PORT_DEFAULT, ERRAND_PORT_DEFAULT, DOG_PORT_DEFAULT = 8120, 8110, 8111
AUTHOR_DEFAULT = "zach"
TEXT_BASE_DEFAULT = "tell Grandma to charge your phone"
MOCK_TRANSCRIPT_DEFAULT = "okay thank you"
REQUIRED_EVENTS = ("navigating", "arrived", "speaking", "listening", "heard", "completed")
RUN_TERMINAL = {"completed", "failed", "unreachable", "cancelled"}


def http_req(url, method="GET", body=None, headers=None, timeout=8.0):
    """(status, parsed-json-or-raw) over the real transport; never raises on HTTP errors."""
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


def norm_text(value):
    """Lowercase, collapse whitespace, drop punctuation: compares transcripts by words."""
    out, prev_space = [], False
    for ch in (value or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        elif not prev_space and out:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


class Rehearsal:
    """One bounded rehearsal; tests inject `req_fn` (fake HTTP) and `clock`."""

    def __init__(self, req_fn, *, app, errand, dog, deliveries=5, author=AUTHOR_DEFAULT,
                 text_base=TEXT_BASE_DEFAULT, mock_transcript=MOCK_TRANSCRIPT_DEFAULT,
                 api_token="", run_timeout_s=180.0, gate_wait_s=90.0, execute_wait_s=45.0,
                 no_replay_window_s=5.0, poll_s=1.0, http_timeout_s=8.0,
                 clock=time.monotonic, sleeper=time.sleep):
        self.req, self.app, self.errand, self.dog = req_fn, app, errand, dog
        self.deliveries, self.author = deliveries, author
        self.text_base, self.mock_transcript = text_base, mock_transcript
        self.api_token = api_token
        self.run_timeout_s, self.gate_wait_s, self.execute_wait_s = run_timeout_s, gate_wait_s, execute_wait_s
        self.no_replay_window_s, self.poll_s, self.http_timeout_s = no_replay_window_s, poll_s, http_timeout_s
        self.clock, self.sleeper = clock, sleeper
        self.failures = []

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_token} if self.api_token else {}

    def get(self, url):
        return self.req(url, timeout=self.http_timeout_s)

    def post(self, url, body):
        return self.req(url, method="POST", body=body, headers=self._headers(),
                        timeout=self.http_timeout_s)

    def fail(self, scope, reason):
        self.failures.append({"scope": scope, "reason": reason})
        print(f"  [FAIL] {scope}: {reason}")

    def wait_for(self, check, timeout_s):
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            result = check()
            if result:
                return result
            self.sleeper(self.poll_s)
        return None

    # ---- gate ----------------------------------------------------------------
    def gate(self):
        print("== simulation gate (no POST until this passes)")
        deadline = self.clock() + self.gate_wait_s
        tel = app_status = voice = None
        while True:
            _, tel = self.get(self.dog + "/telemetry.json")
            _, app_status = self.get(self.app + "/api/dog/status")
            _, voice = self.get(self.dog + "/voice")
            ok, reasons = simulation_gate(tel, app_status, voice)
            if ok:
                break
            if self.clock() >= deadline:
                for reason in reasons:
                    self.fail("simulation gate", reason)
                self.fail("simulation gate",
                          f"not a verified simulated stack within {self.gate_wait_s:g}s; refusing every POST")
                return False, {"passed": False,
                               "failure_reasons": [f["reason"] for f in self.failures]}
            self.sleeper(self.poll_s)
        print("  [PASS] source=simulation, connected, mocked voice")
        return True, {"passed": True,
                      "telemetry_source": (tel or {}).get("source"),
                      "app_dog_status_source": (app_status or {}).get("source"),
                      "voice": {"source": (voice or {}).get("source"),
                                "mocked": (voice or {}).get("mocked")},
                      "failure_reasons": []}

    # ---- telemetry evidence (direct samples; displacement labelled inferred) ---
    def telemetry_sample(self):
        st, status = self.get(self.dog + "/status.json")
        _, space = self.get(self.dog + "/spacetime.json")
        status = status if isinstance(status, dict) else {}
        pose = status.get("pose") if isinstance(status.get("pose"), dict) else None
        counts = (space or {}).get("counts") if isinstance(space, dict) else None
        return {
            "sampled": st == 200,
            "pose": {k: pose.get(k) for k in ("x", "y", "yaw")} if pose else None,
            "t_s": status.get("t_s"),
            "track_count": len(status.get("tracks") or []),
            "fps": status.get("fps"),
            "spacetime_counts": dict(counts) if isinstance(counts, dict) else None,
        }

    # ---- messages --------------------------------------------------------------
    def post_message(self, text):
        st, receipt = self.post(self.app + "/api/messages",
                                {"author_id": self.author, "text": text})
        if st != 202 or not isinstance(receipt, dict) or not isinstance(receipt.get("run_id"), str):
            return None, f"POST /api/messages not accepted (HTTP {st}, body={receipt})"
        if receipt.get("deduplicated"):
            return None, (f"server deduplicated onto existing run {receipt.get('run_id')}; "
                          "a fresh run was required")
        return receipt["run_id"], None

    def post_message_allow_dedup(self, text):
        """Like post_message, but a deduplicated response is a success that
        returns (existing run_id, None). Used by the pause drill, where the
        duplicate request is EXPECTED to reuse the in-flight run."""
        st, receipt = self.post(self.app + "/api/messages",
                                {"author_id": self.author, "text": text})
        if st != 202 or not isinstance(receipt, dict) or not isinstance(receipt.get("run_id"), str):
            return None, f"POST /api/messages not accepted (HTTP {st}, body={receipt})"
        return receipt["run_id"], None

    def run_state(self, run_id):
        st, run = self.get(self.app + f"/api/runs/{run_id}")
        if st == 200 and isinstance(run, dict):
            return run
        return None

    def poll_terminal(self, run_id, timeout_s):
        def terminal():
            run = self.run_state(run_id)
            if run and run.get("status") in RUN_TERMINAL:
                return run
            return None
        return self.wait_for(terminal, timeout_s)

    def validate_completed_run(self, run):
        """completed status, required events in order, mock transcript heard."""
        events = run.get("events") or []
        kinds = [e.get("kind") for e in events]
        reasons = []
        missing = [k for k in REQUIRED_EVENTS if k not in kinds]
        if missing:
            reasons.append(f"missing events {missing}")
        idx = [kinds.index(k) for k in REQUIRED_EVENTS if k in kinds]
        if idx != sorted(idx):
            reasons.append(f"required events out of order: {kinds}")
        heard = [str((e.get("payload") or {}).get("transcript") or "")
                 for e in events if e.get("kind") == "heard"]
        if not any(norm_text(h) == norm_text(self.mock_transcript) for h in heard):
            reasons.append(f"no heard transcript matching '{self.mock_transcript}' (heard={heard})")
        if run.get("status") != "completed":
            reasons.append(f"terminal status is {run.get('status')!r}, expected 'completed'")
        return reasons

    def one_delivery(self, index, text):
        print(f"== delivery {index + 1}/{self.deliveries}")
        started = self.clock()
        entry = {"index": index, "text": text, "run_id": None, "status": None,
                 "events": [], "heard_transcripts": [], "elapsed_s": None,
                 "failure_reasons": []}
        run_id, err = self.post_message(text)
        if err:
            self.fail(f"delivery {index}", err)
            entry["failure_reasons"].append(err)
            return entry
        entry["run_id"] = run_id
        print(f"  run {run_id} accepted")
        run = self.poll_terminal(run_id, self.run_timeout_s)
        entry["elapsed_s"] = round(self.clock() - started, 3)
        if run is None:
            err = f"run did not reach a terminal state within {self.run_timeout_s:g}s"
            self.fail(f"delivery {index}", err)
            entry["failure_reasons"].append(err)
            return entry
        entry["status"] = run.get("status")
        entry["events"] = [e.get("kind") for e in (run.get("events") or [])]
        entry["heard_transcripts"] = [str((e.get("payload") or {}).get("transcript") or "")
                                      for e in (run.get("events") or [])
                                      if e.get("kind") == "heard"]
        for reason in self.validate_completed_run(run):
            self.fail(f"delivery {index}", reason)
            entry["failure_reasons"].append(reason)
        if not entry["failure_reasons"]:
            print(f"  [PASS] completed with events {entry['events']}")
        return entry

    # ---- pause drill ------------------------------------------------------------
    def pause_drill(self, counter):
        print("== pause drill (executing + queued cancel, correlated stop, no replay)")
        result = {"duplicate_run_id_matches": None, "queued_run_id": None,
                  "cancelled_runs": [], "stop_confirmed": None, "paused": None,
                  "observed_replay": False, "statuses_after_pause": {},
                  "fresh_run_id": None, "elapsed_s": None, "failure_reasons": []}
        started = self.clock()

        # Baseline: run ids that already exist (the deliveries' completed runs).
        # Only ids NOT in this baseline count as post-pause replays.
        st, snap = self.get(self.app + "/api/family/snapshot")
        runs = (snap or {}).get("runs") if isinstance(snap, dict) else None
        baseline_ids = {r.get("run_id") for r in (runs or []) if isinstance(r, dict)} if isinstance(runs, list) else set()
        if not isinstance(runs, list):
            self.fail("pause drill", "GET /api/family/snapshot did not return a runs list before the drill")

        def pfail(reason):
            self.fail("pause drill", reason)
            result["failure_reasons"].append(reason)

        executing_text = f"{self.text_base} [pause drill executing {next(counter)}]"
        run_id, err = self.post_message(executing_text)
        if err:
            pfail(err)
            return result

        def executing():
            run = self.run_state(run_id)
            if run and (run.get("status") in ("dispatched", "queued", "running")
                        or run.get("events")):
                return run
            return None
        if self.wait_for(executing, self.execute_wait_s) is None:
            pfail(f"run {run_id} showed no execution progress within {self.execute_wait_s:g}s")
            return result

        # Duplicate in-flight request must reuse the SAME run id (server dedup).
        dup_id, err = self.post_message_allow_dedup(executing_text)
        if err is None:
            result["duplicate_run_id_matches"] = dup_id == run_id
            if dup_id != run_id:
                pfail(f"duplicate in-flight request returned run {dup_id}, expected the same {run_id}")
        else:
            pfail(f"duplicate in-flight request failed: {err}")

        # A second distinct run must queue behind the executing one, then both cancel.
        queued_text = f"{self.text_base} [pause drill queued {next(counter)}]"
        queued_id, err = self.post_message(queued_text)
        if err:
            pfail(f"queued request failed: {err}")
        else:
            result["queued_run_id"] = queued_id

        st, paused = self.post(self.app + "/api/family/pause", {})
        result["elapsed_s"] = round(self.clock() - started, 3)
        if st != 200 or not isinstance(paused, dict):
            pfail(f"POST /api/family/pause failed (HTTP {st}, body={paused})")
            return result
        result["paused"] = paused.get("paused")
        result["stop_confirmed"] = paused.get("stop_confirmed")
        result["cancelled_runs"] = list(paused.get("cancelled_runs") or [])
        if paused.get("paused") is not True:
            pfail(f"pause receipt paused={paused.get('paused')!r}, expected true")
        if paused.get("stop_confirmed") is not True:
            pfail("stop_confirmed is not true: the body stop was not acknowledged")
        for rid in (run_id, queued_id):
            if rid and rid not in result["cancelled_runs"]:
                pfail(f"run {rid} missing from cancelled_runs")
        print(f"  pause receipt: paused={result['paused']} "
              f"stop_confirmed={result['stop_confirmed']} cancelled={len(result['cancelled_runs'])}")

        for rid in (run_id, queued_id):
            if not rid:
                continue
            run = self.poll_terminal(rid, self.execute_wait_s)
            status = (run or {}).get("status")
            result["statuses_after_pause"][rid] = status
            if status != "cancelled":
                pfail(f"run {rid} is {status!r} after pause, expected 'cancelled'")

        # No automatic replay: known runs stay cancelled; no new run appears.
        # No automatic replay: any run id created after the pause (not in the
        # pre-pause baseline) is a replay; pre-existing runs may legitimately
        # be present (e.g. completed deliveries).
        known = {rid for rid in (run_id, queued_id) if rid}
        deadline = self.clock() + self.no_replay_window_s
        while self.clock() < deadline:
            st, snap = self.get(self.app + "/api/family/snapshot")
            runs = (snap or {}).get("runs") if isinstance(snap, dict) else None
            if not isinstance(runs, list):
                pfail("GET /api/family/snapshot did not return a runs list")
                break
            for run in runs:
                rid = run.get("run_id")
                if rid not in known and rid not in baseline_ids:
                    result["observed_replay"] = True
                    pfail(f"unexpected run {rid} appeared after pause "
                          f"(status={run.get('status')!r}): automatic replay")
                elif rid in known and run.get("status") != "cancelled":
                    result["observed_replay"] = True
                    pfail(f"run {rid} left cancelled after pause (status={run.get('status')!r})")
            if result["observed_replay"]:
                break
            self.sleeper(self.poll_s)

        # One fresh explicit request resumes service and must complete normally.
        fresh_text = f"{self.text_base} [fresh after pause {next(counter)}]"
        fresh_id, err = self.post_message(fresh_text)
        if err:
            pfail(f"fresh request after pause failed: {err}")
        else:
            result["fresh_run_id"] = fresh_id
            if fresh_id in (run_id, queued_id):
                pfail("fresh request reused a cancelled run_id")
            run = self.poll_terminal(fresh_id, self.run_timeout_s)
            if run is None:
                pfail(f"fresh run {fresh_id} did not finish within {self.run_timeout_s:g}s")
            else:
                for reason in self.validate_completed_run(run):
                    pfail(f"fresh run: {reason}")
        return result

    # ---- whole rehearsal ----------------------------------------------------------
    def run(self):
        started = self.clock()
        summary = {
            "cli": "robot/rehearse_sim.py",
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "simulation",
            "model": "not inspected by this probe; the standard demo_sim launcher disables inference providers",
            "audio": f"mock: dog /voice reports mocked simulation audio; expected reply '{self.mock_transcript}'",
            "motion_claim": "none: telemetry samples are recorded evidence; acceptance asserts the delivery workflow, not locomotion",
            "config": {"app": self.app, "errand": self.errand, "dog": self.dog,
                       "deliveries": self.deliveries, "author": self.author,
                       "text_base": self.text_base, "mock_transcript": self.mock_transcript,
                       "run_timeout_s": self.run_timeout_s,
                       "api_token_used": bool(self.api_token)},
            "gate": None, "deliveries": [], "pause_drill": None,
            "telemetry": {"note": "direct sampled evidence; displacement below is inferred, not measured locomotion",
                          "direct_samples": {}, "inferred_movement": None},
            "failure_reasons": [], "exit_code": 1, "elapsed_total_s": None,
        }
        gate_ok, gate_report = self.gate()
        summary["gate"] = gate_report
        if not gate_ok:
            summary["failure_reasons"] = [f["reason"] for f in self.failures]
            summary["elapsed_total_s"] = round(self.clock() - started, 3)
            return summary

        summary["telemetry"]["direct_samples"]["before"] = self.telemetry_sample()

        counter = iter(range(1, 10000))
        completed = 0
        for index in range(self.deliveries):
            text = f"{self.text_base} [rehearsal {next(counter)}]"
            entry = self.one_delivery(index, text)
            summary["deliveries"].append(entry)
            if not entry["failure_reasons"]:
                completed += 1
        if completed < self.deliveries:
            self.fail("deliveries", f"only {completed}/{self.deliveries} deliveries satisfied acceptance")

        summary["pause_drill"] = self.pause_drill(counter)

        after = self.telemetry_sample()
        summary["telemetry"]["direct_samples"]["after"] = after
        before = summary["telemetry"]["direct_samples"]["before"]
        if before.get("pose") and after.get("pose"):
            try:
                dx = after["pose"]["x"] - before["pose"]["x"]
                dy = after["pose"]["y"] - before["pose"]["y"]
                summary["telemetry"]["inferred_movement"] = {
                    "method": "pose difference between start/end samples; inferred, not a locomotion measurement",
                    "pose_displacement_m": round((dx * dx + dy * dy) ** 0.5, 4),
                }
            except (TypeError, KeyError):
                pass

        summary["failure_reasons"] = [f["reason"] for f in self.failures]
        summary["exit_code"] = 1 if self.failures else 0
        summary["elapsed_total_s"] = round(self.clock() - started, 3)
        verdict = f"FAILURES: {len(self.failures)}" if self.failures else "no failures"
        print(f"== summary: {completed}/{self.deliveries} deliveries, {verdict}, "
              f"{summary['elapsed_total_s']:g}s total")
        return summary


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="bounded synthetic E2E rehearsal (simulated stack, fail-closed gate)")
    p.add_argument("--app-port", type=int, default=APP_PORT_DEFAULT)
    p.add_argument("--errand-port", type=int, default=ERRAND_PORT_DEFAULT)
    p.add_argument("--dog-port", type=int, default=DOG_PORT_DEFAULT)
    p.add_argument("--deliveries", type=int, default=5)
    p.add_argument("--author", default=AUTHOR_DEFAULT)
    p.add_argument("--text-base", default=TEXT_BASE_DEFAULT)
    p.add_argument("--mock-transcript", default=MOCK_TRANSCRIPT_DEFAULT)
    p.add_argument("--api-token", default="", help="family bearer token; empty for the loopback demo")
    p.add_argument("--run-timeout", type=float, default=180.0, help="per-run terminal-state bound (s)")
    p.add_argument("--gate-wait", type=float, default=90.0, help="gate readiness bound (s)")
    p.add_argument("--no-replay-window", type=float, default=5.0, help="post-pause observation window (s)")
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--summary-out", default=None, help="summary JSON path (default .data/sim/rehearse-<ts>.json)")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not 1 <= args.deliveries <= 20:
        print("--deliveries must be >= 1 and <= 20", file=sys.stderr)
        return 2
    bounds = (("run_timeout", 1, 600), ("gate_wait", 0.1, 120),
              ("no_replay_window", 0.1, 30), ("poll", 0.02, 5))
    for key, low, high in bounds:
        value = getattr(args, key)
        if not math.isfinite(value) or not low <= value <= high:
            print(f"--{key.replace('_', '-')} must be between {low} and {high}", file=sys.stderr)
            return 2
    rehearsal = Rehearsal(
        http_req,
        app=f"http://127.0.0.1:{args.app_port}",
        errand=f"http://127.0.0.1:{args.errand_port}",
        dog=f"http://127.0.0.1:{args.dog_port}",
        deliveries=args.deliveries, author=args.author, text_base=args.text_base,
        mock_transcript=args.mock_transcript, api_token=args.api_token,
        run_timeout_s=args.run_timeout, gate_wait_s=args.gate_wait,
        no_replay_window_s=args.no_replay_window, poll_s=args.poll,
    )
    summary = rehearsal.run()
    out_path = Path(args.summary_out) if args.summary_out else (
        ROOT / ".data" / "sim" / ("rehearse-" + time.strftime("%Y%m%d-%H%M%S") + ".json"))
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"summary written: {out_path}")
    except OSError as exc:
        print(f"could not write summary {out_path}: {exc}", file=sys.stderr)
        return 1
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
