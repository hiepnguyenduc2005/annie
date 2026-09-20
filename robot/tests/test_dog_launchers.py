"""Exercise the actual hardware launcher with synthetic child services; no network or robot."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def test_launcher_shell_syntax():
    for script in ("demo_dog.sh", "gx10_setup.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "robot" / script)], check=True)


def _write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(0o755)


def _sandbox(tmp_path, reason=None):
    _write(tmp_path / "robot/demo_dog.sh", (ROOT / "robot/demo_dog.sh").read_text())
    _write(tmp_path / ".env", "ANNIE_INTERNAL_SECRET=synthetic\nUNITREE_AES_128_KEY=synthetic\n")
    for name, script in {
        "ifconfig": "printf 'inet 172.20.10.9 netmask fake\\n'",
        "ping": "exit 0", "lsof": "exit 1", "curl": "exit 1",
    }.items():
        _write(tmp_path / "bin" / name, "#!/bin/sh\n" + script + "\n", True)
    service = '''import json, os, signal, sys, time
from pathlib import Path
role = Path(sys.argv[0]).name
root = Path(os.environ["LAUNCHER_TEST_ROOT"])
(root / (role + ".pid")).write_text(str(os.getpid()))
def stop(sig, frame):
    (root / (role + ".stopped")).write_text(str(sig))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
if role == "go2_patrol_greet.py" and os.environ.get("TEST_REASON"):
    with (root / "launches").open("a") as f: f.write("run\\n")
    out = Path(sys.argv[sys.argv.index("--output") + 1])
    out.write_text(json.dumps({"reason": os.environ["TEST_REASON"]}))
    raise SystemExit(0)
while True: time.sleep(0.05)
'''
    _write(tmp_path / "robot/go2_patrol_greet.py", service)
    _write(tmp_path / "robot/go2_errand.py", service)
    _write(tmp_path / ".venv/bin/uvicorn", f"#!{sys.executable}\n" + service, True)
    for relative in (".venv/bin/python", ".cache/dimos/.venv/bin/python"):
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to(sys.executable)
    env = dict(os.environ, PATH=str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
               LAUNCHER_TEST_ROOT=str(tmp_path), TEST_REASON=reason or "", PING_CMD="ping")
    return subprocess.Popen(["bash", str(tmp_path / "robot/demo_dog.sh")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _cleanup(proc):
    # Only this test's process group; ensure failure cannot orphan synthetic services.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def test_actual_launcher_stops_owned_children_and_preserves_unrelated_process(tmp_path):
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc = _sandbox(tmp_path)
    roles = ("uvicorn", "go2_errand.py", "go2_patrol_greet.py")
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / (role + ".pid")).exists() for role in roles):
            assert proc.poll() is None, proc.communicate()[0]
            assert time.monotonic() < deadline, "synthetic services did not start"
            time.sleep(0.05)
        pids = [int((tmp_path / (role + ".pid")).read_text()) for role in roles]
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=18)
        assert proc.returncode == 143, output
        assert all(not _alive(pid) for pid in pids), output
        assert all((tmp_path / (role + ".stopped")).exists() for role in roles), output
        assert survivor.poll() is None, "an unrelated process was stopped"
    finally:
        _cleanup(proc)
        survivor.terminate()
        survivor.wait(timeout=5)


def test_battery_floor_ends_without_relaunching(tmp_path):
    proc = _sandbox(tmp_path, "battery_low")
    try:
        output, _ = proc.communicate(timeout=18)
        assert proc.returncode == 0, output
        assert (tmp_path / "launches").read_text().splitlines() == ["run"]
        assert "battery floor reached" in output
    finally:
        _cleanup(proc)


def test_missing_environment_fails_before_any_service_starts(tmp_path):
    _write(tmp_path / "robot/demo_dog.sh", (ROOT / "robot/demo_dog.sh").read_text())
    result = subprocess.run(["bash", str(tmp_path / "robot/demo_dog.sh")], capture_output=True, text=True)
    assert result.returncode == 2 and ".env not found" in result.stdout
    assert not list(tmp_path.glob("*.pid"))
