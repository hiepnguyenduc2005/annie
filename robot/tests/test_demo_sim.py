"""Unit tests for the offline demo launcher (robot/demo_sim.py).

Meaningful lifecycle/env coverage without launching real services: env
sanitization, port validation, preflight failures, readiness timeout, and the
supervisor's process-group stop on already-dead children.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import demo_sim  # noqa: E402
import verify_sim  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def sim_root(tmp_path):
    return tmp_path / "sim"


# --- env -------------------------------------------------------------------

def test_env_strips_inherited_credentials(sim_root):
    import os
    monkey_keys = {"ELEVENLABS_API_KEY": "sk", "DEEPGRAM_API_KEY": "dg",
                   "OPENAI_API_KEY": "o",
                   "SOME_SERVICE_API_KEY": "zzz", "MY_TOOL_ACCESS_TOKEN": "t", "OTHER_SERVICE_TOKEN": "t",
                   "ANNIE_INTERNAL_SECRET": "old", "ANNIE_API_TOKEN": "old",
                   "MONGODB_URI": "mongodb://x"}
    saved = {k: os.environ.get(k) for k in monkey_keys}
    try:
        os.environ.update(monkey_keys)
        env = demo_sim.build_env(app_port=1, errand_port=2, dog_port=3, sim_root=sim_root)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for k in monkey_keys:
        if k in ("MONGODB_URI", "ANNIE_INTERNAL_SECRET", "ANNIE_API_TOKEN"):
            continue  # re-set fresh below; only the inherited value must be gone
        assert k not in env
    assert env["MONGODB_URI"] == ""  # inherited value dropped, memory fallback
    assert env["ANNIE_INTERNAL_SECRET"] != "old"
    assert env["ANNIE_LLM_PROVIDER"] == "off"
    assert env["ANNIE_VOICE_CLOUD"] == "0"
    assert env["MONGODB_URI"] == ""
    assert env["NO_PROXY"] == "*" and env["no_proxy"] == "*"
    assert env["ANNIE_INTERNAL_SECRET"] not in ("", "old")
    assert env["ANNIE_API_TOKEN"] == ""  # loopback-only mode


def test_env_paths_are_unique_per_sim_root(sim_root):
    env = demo_sim.build_env(app_port=1, errand_port=2, dog_port=3, sim_root=sim_root)
    assert env["ANNIE_DB_PATH"] == str(sim_root / "annie.sqlite3")
    assert env["ANNIE_FACES_DIR"] == str(sim_root / "faces")


# --- argument validation ---------------------------------------------------

def test_main_rejects_duplicate_ports(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        demo_sim.main(["--app-port", "8000", "--errand-port", "8000"])
    assert "must differ" in capsys.readouterr().err


def test_main_rejects_out_of_range_port():
    with pytest.raises(SystemExit):
        demo_sim.main(["--app-port", "80"])


def test_main_rejects_absurd_duration():
    with pytest.raises(SystemExit):
        demo_sim.main(["--duration", "1"])


# --- preflight -------------------------------------------------------------

def test_preflight_fails_on_occupied_port(tmp_path):
    port = free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        errors = demo_sim.preflight([port], Path("/nonexistent/python"), Path("/nonexistent/dimos"))
    assert any("already in use" in e for e in errors)


def test_preflight_fails_missing_venvs(tmp_path):
    errors = demo_sim.preflight([free_port()], tmp_path / "nope", tmp_path / "nada")
    assert any("missing" in e for e in errors)
    assert any("simulator venv missing" in e for e in errors)


# --- readiness / supervisor ------------------------------------------------

def test_wait_ready_times_out():
    start = time.monotonic()
    assert demo_sim.wait_ready(lambda: False, 0.5, "x", poll_s=0.1) is False
    assert time.monotonic() - start >= 0.4


def test_wait_ready_success_immediately():
    assert demo_sim.wait_ready(lambda: True, 1.0, "x", poll_s=0.05) is True


def test_stack_stop_only_touches_own_children(tmp_path):
    stack = demo_sim.Stack(tmp_path / "logs")
    # A real but harmless child (sleep), in its own process group, plus an
    # already-dead entry: stop() must not raise on either.
    stack.spawn("sleeper", [sys.executable, "-c", "import time; time.sleep(30)"], {})
    stack.procs["dead"] = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.2)  # let /bin/true exit
    stack.stop()
    assert stack.procs["sleeper"].poll() is not None
    assert stack.procs["dead"].poll() is not None


def test_stack_exited_reports_dead_child(tmp_path):
    stack = demo_sim.Stack(tmp_path / "logs")
    stack.spawn("quick", [sys.executable, "-c", "pass"], {})
    deadline = time.monotonic() + 5
    while stack.exited() == [] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stack.exited() == ["quick"]


# --- verify_sim simulation gate ---------------------------------------------

SIM_TEL = {"source": "simulation", "connected": True}
SIM_APP = {"source": "simulation", "available": True, "connected": True}
SIM_VOICE = {"source": "simulation", "mocked": True}


def test_simulation_gate_accepts_mock_stack():
    gate_ok, reasons = verify_sim.simulation_gate(SIM_TEL, SIM_APP, SIM_VOICE)
    assert gate_ok and reasons == []


@pytest.mark.parametrize("tel,app,voice", [
    ({"source": "hardware", "connected": True}, SIM_APP, SIM_VOICE),
    ({"connected": True}, SIM_APP, SIM_VOICE),                # source missing
    ({}, SIM_APP, SIM_VOICE),
    (None, SIM_APP, SIM_VOICE),
    (SIM_TEL, {"source": "hardware", "available": True, "connected": True}, SIM_VOICE),
    (SIM_TEL, {"source": "simulation"}, SIM_VOICE),              # available/connected missing
    (SIM_TEL, {"source": "simulation", "available": False, "connected": True}, SIM_VOICE),
    (SIM_TEL, {"source": "simulation", "available": True, "connected": False}, SIM_VOICE),
    (SIM_TEL, SIM_APP, {"source": "simulation"}),              # mocked missing
    (SIM_TEL, SIM_APP, {"source": "hardware", "mocked": True}),
    (SIM_TEL, SIM_APP, None),
])
def test_simulation_gate_refuses_non_simulated(tel, app, voice):
    gate_ok, reasons = verify_sim.simulation_gate(tel, app, voice)
    assert not gate_ok and reasons


def test_probe_refuses_to_post_when_gate_fails(monkeypatch, capsys):
    """On a hardware/missing-source stack the probe must exit 1 having issued
    zero state-changing POSTs (only the read-only GETs are allowed)."""
    posts = []
    real_req = verify_sim.req

    def recording_req(url, method="GET", body=None, headers=None, timeout=8.0):
        if method == "POST":
            posts.append((url, body))
            # Fail loudly if a POST ever slips through the gate.
            raise AssertionError(f"POST issued before gate: {url} {body}")
        if url.endswith("/telemetry.json"):
            return 200, {"source": "hardware", "connected": True}
        if url.endswith("/api/dog/status"):
            return 200, {"source": "hardware", "available": True}
        if url.endswith("/voice"):
            return 200, {"source": "hardware", "mocked": False}
        return 200, {"ok": True}

    monkeypatch.setattr(verify_sim, "req", recording_req)
    rc = verify_sim.main(["--app-port", "0", "--errand-port", "0", "--dog-port", "0"])
    assert rc == 1
    assert posts == []  # no state-changing request reached the transport
    out = capsys.readouterr().out
    assert "REFUSING" in out


def test_sim_root_relative_resolves_against_repo_root(tmp_path, monkeypatch):
    # A caller in any cwd gets repo-anchored data paths.
    monkeypatch.chdir(tmp_path)
    sim_root = tmp_path / "sr"
    env = demo_sim.build_env(app_port=1, errand_port=2, dog_port=3, sim_root=sim_root)
    assert env["ANNIE_DB_PATH"] == str(sim_root / "annie.sqlite3")


@pytest.mark.parametrize("relay_succeeds", [True, False])
def test_probe_reports_relay_outcome_without_autonomous_conversations(monkeypatch, capsys, relay_succeeds):
    """Optional autonomous activity cannot crash or conceal the actual relay result."""
    def responding_req(url, method="GET", body=None, **kwargs):
        if url.endswith("/telemetry.json"):
            return 200, SIM_TEL
        if url.endswith("/api/dog/status"):
            return 200, SIM_APP
        if url.endswith("/voice"):
            return 200, SIM_VOICE
        if url.endswith("/command") and method == "POST":
            return 202, {"state": "accepted"}
        if "/command/" in url:
            return 200, {"state": "completed", "result": {
                "source": "simulation", "where": "simulation mock", "played": True}}
        if url.endswith("/api/messages"):
            return 202, {"run_id": "test-relay"}
        if "/api/runs/" in url:
            events = [{"kind": kind, "payload": {"transcript": "okay thank you"}}
                      for kind in verify_sim.EXPECTED_RELAY_EVENTS] if relay_succeeds else []
            return 200, {"status": "completed" if relay_succeeds else "failed", "events": events}
        return 200, {"ok": True}

    monkeypatch.setattr(verify_sim, "req", responding_req)
    assert verify_sim.main([]) == (0 if relay_succeeds else 1)
    assert len(verify_sim.SKIP) == 2
    assert "2 skip" in capsys.readouterr().out


def test_main_sim_root_flag_resolves_relative_to_repo(monkeypatch, tmp_path):
    """--sim-root demo-sim/x from another cwd lands under the repo, not cwd."""
    monkeypatch.chdir(tmp_path)
    captured = {}

    real_preflight = demo_sim.preflight

    def fake_preflight(ports, venv_py, dimos_py):
        return []

    class FakeProc:
        pid = 4242

        def poll(self):
            return 0  # pretend the child exited: main tears down and returns 1

    class FakeStack:
        def __init__(self, log_dir):
            captured["log_dir"] = log_dir
            self.log_dir = log_dir
            self.procs = {}

        def spawn(self, name, argv, env):
            self.procs[name] = FakeProc()

        def exited(self):
            return ["app"]

        def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr(demo_sim, "preflight", fake_preflight)
    monkeypatch.setattr(demo_sim, "Stack", FakeStack)
    rc = demo_sim.main(["--sim-root", "demo-sim/x", "--app-port", str(free_port()),
                        "--errand-port", str(free_port()), "--dog-port", str(free_port())])
    assert rc == 1 and captured.get("stopped") is True
    assert str(captured["log_dir"]).startswith(str(demo_sim.ROOT))
