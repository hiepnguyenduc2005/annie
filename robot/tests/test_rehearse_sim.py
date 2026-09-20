"""Tests for robot/rehearse_sim.py: gate-before-POST, timeout/failure paths,
and the full delivery + pause flow over a fake HTTP transport (no live services)."""
import json
import time
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot.rehearse_sim import Rehearsal, http_req, main, norm_text


GOOD_TEL = {"source": "simulation", "connected": True}
GOOD_APP_STATUS = {"source": "simulation", "available": True, "connected": True}
GOOD_VOICE = {"source": "simulation", "mocked": True}
GOOD_STATUS = {"pose": {"x": 1.0, "y": 2.0, "yaw": 0.0}, "t_s": 12.0,
               "tracks": [{"track_id": 1}], "fps": 14.0}
GOOD_SPACE = {"counts": {"frames": 10, "poses": 20, "people": 2}}
REQUIRED = ("navigating", "arrived", "speaking", "listening", "heard", "completed")
MOCK = "okay thank you"


class FakeHTTP:
    """Scripted app/errand/dog services: records every request, drives run
    progression as the CLI polls. Gate must pass before any POST arrives."""

    def __init__(self, *, tel=None, app_status=None, voice=None, status=None,
                 space=None, complete=True, run_events=True):
        self.calls = []
        self.tel = tel if tel is not None else GOOD_TEL
        self.app_status = app_status if app_status is not None else GOOD_APP_STATUS
        self.voice = voice if voice is not None else GOOD_VOICE
        self.status = status if status is not None else GOOD_STATUS
        self.space = space if space is not None else GOOD_SPACE
        self.complete = complete          # completed vs failed terminal event
        self.run_events = run_events      # include required events in runs
        self.runs = {}
        self.next_id = 0
        self.post_seen = False
        self.pause_requested = False

    def __call__(self, url, method="GET", body=None, headers=None, timeout=8.0):
        self.calls.append((method, url, body))
        if "/api/runs/" in url and method == "GET":
            run_id = url.rsplit("/", 1)[-1]
            run = self.runs.get(run_id)
            if run is None:
                return 404, None
            if run["status"] == "running" and "pause drill" not in run["text"]:
                self._progress(run_id, "running", kind="arrived")
                self._progress(run_id, "running", kind="speaking")
                self._progress(run_id, "running", kind="listening")
                self._progress(run_id, "running", kind="heard",
                               payload={"transcript": MOCK})
                self._progress(run_id, "running",
                               kind="completed" if self.complete else "failed",
                               payload={} if self.complete else {"error": "synthetic failure"})
                run["status"] = "completed" if self.complete else "failed"
            return 200, run
        if url.endswith("/telemetry.json"):
            return 200, self.tel
        if url.endswith("/api/dog/status"):
            return 200, self.app_status
        if url.endswith("/voice"):
            return 200, self.voice
        if url.endswith("/status.json"):
            return 200, self.status
        if url.endswith("/spacetime.json"):
            return 200, self.space
        if url.endswith("/api/messages") and method == "POST":
            self.post_seen = True
            existing = next((r for r in self.runs.values()
                             if r["status"] not in ("completed", "failed", "cancelled")
                             and r["text"] == body["text"]), None)
            if existing is not None:
                return 202, {"run_id": existing["run_id"], "status": existing["status"],
                             "deduplicated": True}
            self.pause_requested = False
            self.next_id += 1
            run_id = f"run-{self.next_id}"
            self.runs[run_id] = {"run_id": run_id, "text": body["text"],
                                 "status": "dispatched", "events": []}
            self._progress(run_id, "running", kind="navigating")
            return 202, {"run_id": run_id, "status": "running"}
        if "/api/runs/" in url and method == "GET":
            run_id = url.rsplit("/", 1)[-1]
            run = self.runs.get(run_id)
            if run is None:
                return 404, None
            if run["status"] == "running" and "pause drill" not in run["text"]:
                self._progress(run_id, "running", kind="arrived")
                self._progress(run_id, "running", kind="speaking")
                self._progress(run_id, "running", kind="listening")
                self._progress(run_id, "running", kind="heard",
                               payload={"transcript": MOCK})
                self._progress(run_id, "running", kind="completed" if self.complete else "failed",
                               payload={} if self.complete else {"error": "synthetic failure"})
                run["status"] = "completed" if self.complete else "failed"
            return 200, run
        if url.endswith("/api/family/pause") and method == "POST":
            self.pause_requested = True
            cancelled = [rid for rid, r in self.runs.items()
                         if r["status"] in ("dispatched", "running", "queued")]
            for rid in cancelled:
                self.runs[rid]["status"] = "cancelled"
            return 200, {"paused": True, "cancelled_runs": cancelled,
                         "stop_confirmed": True}
        if url.endswith("/api/family/snapshot") and method == "GET":
            return 200, {"thread": [], "runs": list(self.runs.values())}
        return 404, None

    def _progress(self, run_id, status, kind, payload=None):
        run = self.runs[run_id]
        run["status"] = status
        if self.run_events:
            run["events"].append({"kind": kind, "payload": payload or {}})

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class FakeClock:
    """Deterministic clock: one tick per poll, so wait_for succeeds promptly."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, dt=0.001):
        self.now += dt


def make(http, deliveries=2, **kw):
    clock = FakeClock()
    def sleeper(_s):
        clock.tick()
    options = dict(deliveries=deliveries, run_timeout_s=5.0, gate_wait_s=2.0,
                   execute_wait_s=5.0, no_replay_window_s=0.5, poll_s=0.0,
                   clock=clock, sleeper=sleeper)
    options.update(kw)
    r = Rehearsal(http, app="http://app", errand="http://errand", dog="http://dog", **options)
    return r, clock


# ---- norm_text ---------------------------------------------------------------------

def test_norm_text_ignores_case_punct_and_whitespace():
    assert norm_text("  Okay,   THANK you... ") == "okay thank you"


# ---- gate before any POST ------------------------------------------------------------

def test_gate_passes_before_any_post_and_records_samples():
    http, _ = FakeHTTP(), None
    r, _ = make(http)
    summary = r.run()
    assert summary["exit_code"] == 0
    first_post_index = next(i for i, c in enumerate(http.calls) if c[0] == "POST")
    gate_gets = [c for c in http.calls[:first_post_index] if c[0] == "GET"]
    assert any(c[1].endswith("/telemetry.json") for c in gate_gets)
    assert any(c[1].endswith("/voice") for c in gate_gets)
    assert summary["gate"]["passed"] is True
    assert summary["telemetry"]["direct_samples"]["before"]["sampled"] is True


@pytest.mark.parametrize("field,value", [
    ("tel", {"source": "hardware", "connected": True}),
    ("tel", {"source": "simulation", "connected": False}),
    ("app_status", {"source": "hardware", "available": True, "connected": True}),
    ("voice", {"source": "simulation", "mocked": False}),
    ("voice", {"source": "hardware", "mocked": True}),
])
def test_gate_failure_sends_no_post_and_fails_closed(field, value):
    http = FakeHTTP(**{field: value})
    r, _ = make(http)
    summary = r.run()
    assert summary["exit_code"] == 1
    assert http.posts() == []
    assert summary["gate"]["passed"] is False
    assert summary["deliveries"] == [] and summary["pause_drill"] is None


def test_gate_timeout_fails_closed_after_wait():
    class NeverReady(FakeHTTP):
        pass
    http = NeverReady(tel={"source": "hardware", "connected": False})
    r, _ = make(http, gate_wait_s=0.05)
    time.sleep(0.1)  # let the real poll loop hit its deadline
    r2, _ = make(http, gate_wait_s=0.05)
    summary = r2.run()
    assert summary["exit_code"] == 1
    assert http.posts() == []


# ---- delivery flow ---------------------------------------------------------------------

def test_two_successful_deliveries_with_required_events():
    http = FakeHTTP(complete=True)
    r, _ = make(http, deliveries=2)
    summary = r.run()
    assert summary["exit_code"] == 0, summary["failure_reasons"]
    assert len(summary["deliveries"]) == 2
    ids = {d["run_id"] for d in summary["deliveries"]}
    assert len(ids) == 2
    for d in summary["deliveries"]:
        assert d["status"] == "completed"
        assert tuple(d["events"])[-6:] == REQUIRED
        assert d["heard_transcripts"] == [MOCK]


def test_delivery_timeout_when_run_never_finishes():
    class Stuck(FakeHTTP):
        def __call__(self, url, method="GET", body=None, headers=None, timeout=8.0):
            if "/api/runs/" in url:
                run_id = url.rsplit("/", 1)[-1]
                return 200, self.runs.get(run_id)
            return super().__call__(url, method=method, body=body,
                                    headers=headers, timeout=timeout)
    http = Stuck(complete=True)
    r, _ = make(http, deliveries=1)
    summary = r.run()
    assert summary["exit_code"] == 1
    assert any("terminal state" in f for f in summary["failure_reasons"])


def test_failed_run_reports_failure_reason():
    http = FakeHTTP(complete=False)
    r, _ = make(http, deliveries=1)
    summary = r.run()
    assert summary["exit_code"] == 1
    assert any("expected 'completed'" in f for f in summary["failure_reasons"])


def test_wrong_transcript_fails():
    http = FakeHTTP()
    http.voice = GOOD_VOICE
    r, _ = make(http, deliveries=1, mock_transcript="a different reply")
    summary = r.run()
    assert summary["exit_code"] == 1
    assert any("no heard transcript" in f for f in summary["failure_reasons"])


def test_dedup_on_delivery_is_a_failure():
    http = FakeHTTP()
    calls = {"n": 0}
    original = http.__call__
    def forced_dedup(url, method="GET", body=None, headers=None, timeout=8.0):
        if url.endswith("/api/messages") and method == "POST":
            calls["n"] += 1
            if calls["n"] == 1:
                return 202, {"run_id": "run-existing", "status": "running",
                             "deduplicated": True}
        return original(url, method=method, body=body, headers=headers, timeout=timeout)
    r, _ = make(forced_dedup, deliveries=1)
    summary = r.run()
    assert summary["exit_code"] == 1
    assert any("deduplicated" in f for f in summary["failure_reasons"])


# ---- pause drill --------------------------------------------------------------------------

def test_pause_drill_full_flow():
    http = FakeHTTP()
    r, _ = make(http, deliveries=1)
    summary = r.run()
    assert summary["exit_code"] == 0, summary["failure_reasons"]
    drill = summary["pause_drill"]
    assert drill["duplicate_run_id_matches"] is True
    assert drill["queued_run_id"] is not None
    assert drill["paused"] is True and drill["stop_confirmed"] is True
    assert set(drill["statuses_after_pause"].values()) == {"cancelled"}
    assert drill["observed_replay"] is False
    assert drill["fresh_run_id"] not in (drill["queued_run_id"],)


def test_pause_drill_detects_replay():
    http = FakeHTTP()
    original = http.__call__
    def replay(url, method="GET", body=None, headers=None, timeout=8.0):
        st, body_out = original(url, method=method, body=body,
                                headers=headers, timeout=timeout)
        if url.endswith("/api/family/snapshot") and method == "GET" and http.pause_requested:
            body_out = dict(body_out)
            body_out["runs"] = body_out["runs"] + [
                {"run_id": "run-replayed", "status": "running", "events": []}]
            return st, body_out
        return st, body_out
    r, _ = make(replay, deliveries=1)
    summary = r.run()
    assert summary["exit_code"] == 1
    assert any("automatic replay" in f for f in summary["failure_reasons"])


# ---- telemetry honesty -------------------------------------------------------------------

def test_telemetry_reports_samples_and_labels_inference():
    http = FakeHTTP()
    r, _ = make(http, deliveries=1)
    summary = r.run()
    tel = summary["telemetry"]
    assert tel["direct_samples"]["before"]["pose"] == {"x": 1.0, "y": 2.0, "yaw": 0.0}
    assert tel["direct_samples"]["before"]["track_count"] == 1
    assert tel["direct_samples"]["before"]["spacetime_counts"]["frames"] == 10
    if tel["inferred_movement"] is not None:
        assert "inferred" in tel["inferred_movement"]["method"]


# ---- CLI entry ----------------------------------------------------------------------------

def test_main_rejects_zero_deliveries(capsys):
    assert main(["--deliveries", "0"]) == 2


def test_main_writes_summary_json_and_fails_closed(tmp_path, monkeypatch):
    import robot.rehearse_sim as mod
    monkeypatch.setattr(mod, "http_req", lambda *a, **k: (0, None))
    out = tmp_path / "summary.json"
    code = main(["--deliveries", "1", "--gate-wait", "0.1",
                 "--summary-out", str(out)])
    assert code == 1
    data = json.loads(out.read_text())
    assert data["exit_code"] == 1
    assert data["gate"]["passed"] is False


def test_main_success_writes_summary(tmp_path, monkeypatch):
    import robot.rehearse_sim as mod
    http = FakeHTTP()
    monkeypatch.setattr(mod, "http_req", http)
    out = tmp_path / "ok.json"
    code = main(["--deliveries", "1", "--summary-out", str(out)])
    assert code == 0, [f for f in json.loads(out.read_text())["failure_reasons"]] if out.exists() else "no file"
    assert json.loads(out.read_text())["exit_code"] == 0
