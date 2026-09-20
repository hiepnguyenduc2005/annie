#!/usr/bin/env python3
"""Offline full-stack launcher: family app + errand + simulated dog, hardware off.

One foreground command brings up the real app_backend, the real errand brain and
the simulated Go2 (MuJoCo apartment, seated resident) on separate loopback ports,
with audio mocked and every provider path disabled. No .env is sourced, inherited
provider credentials and cloud adapters are stripped, MongoDB stays off (memory
fallback), and all data files live under one unique sim root.

    .venv/bin/python robot/demo_sim.py                 # app :8120 errand :8110 dog :8111
    .venv/bin/python robot/demo_sim.py --check         # preflight only, no launch

Ctrl-C stops exactly this launcher's child process groups. If any child exits
unexpectedly the others are stopped, the failed child's log is tailed, and the
launcher exits non-zero. E2E probe for a running stack: robot/verify_sim.py.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PORT_DEFAULT, ERRAND_PORT_DEFAULT, DOG_PORT_DEFAULT = 8120, 8110, 8111

# Provider credentials, cloud adapters, hardware secrets and persistence state
# the offline demo must never inherit from the operator shell.
STRIP_ENV_KEYS = (
    "ANNIE_INTERNAL_SECRET", "ANNIE_API_TOKEN", "ANNIE_BODY_TOKEN",
    "ANNIE_FAMILY_MOCK_ROBOT", "ANNIE_MODE", "ANNIE_DB_PATH",
    "ANNIE_DOG_VIEW_URL", "ANNIE_BODY_URL", "ANNIE_APP_URL",
    "ANNIE_ALLOWED_HOSTS", "ANNIE_VIEW_HOSTS", "ANNIE_FACES_DIR",
    "ANNIE_TARGET", "ANNIE_ES_URL", "ANNIE_ENABLE_CLOUD_AGENTS",
    "ANNIE_LLM_PROVIDER", "ANNIE_VOICE_CLOUD", "ANNIE_MEMORY_RELOAD_S",
    "MONGODB_URI", "MONGODB_DB",
    "SUBCONSCIOUS_API_KEY", "SUBCONSCIOUS_MODEL",
    "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "UNITREE_AES_128_KEY", "GO2_IP",
    # Elastic/search and generic cloud endpoints: inherited tooling must not
    # find a network target to talk to even by accident.
    "ELASTIC_API_KEY", "ES_API_KEY", "ELASTICSEARCH_URL", "ELASTIC_CLOUD_ID",
    "ES_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
)

# Any variable whose name matches this is dropped as well: the demo must not
# inherit a provider credential that a new dependency starts reading tomorrow.
STRIP_ENV_PATTERN = ("API_KEY", "APIKEY", "ACCESS_TOKEN", "REFRESH_TOKEN",
                     "PRIVATE_KEY", "CLOUD_ID")


def build_env(*, app_port: int, errand_port: int, dog_port: int, sim_root: Path) -> dict:
    """Child environment: inherited minus credentials/cloud state, plus loopback
    wiring and unique sim-data paths."""
    env = {k: v for k, v in os.environ.items()
           if k not in STRIP_ENV_KEYS
           and not k.upper().endswith("_TOKEN")
           and not any(p in k.upper() for p in STRIP_ENV_PATTERN)}
    secret = secrets.token_hex(16)  # temp internal secret; never printed or written
    sim_root = str(sim_root)
    env.update({
        "ANNIE_LLM_PROVIDER": "off",        # owner-added: inference off, no network
        "ANNIE_VOICE_CLOUD": "0",
        "ANNIE_ENABLE_CLOUD_AGENTS": "false",
        "MONGODB_URI": "",                  # schema store falls back to memory
        "ANNIE_MODE": "demo",
        "ANNIE_DB_PATH": os.path.join(sim_root, "annie.sqlite3"),
        "ANNIE_FACES_DIR": os.path.join(sim_root, "faces"),
        "ANNIE_INTERNAL_SECRET": secret,
        "ANNIE_API_TOKEN": "",              # loopback-only, unauthenticated family API
        "ANNIE_BODY_TOKEN": "",
        "ANNIE_ALLOWED_HOSTS": "localhost,127.0.0.1,[::1]",
        "ANNIE_VIEW_HOSTS": "127.0.0.1",
        "ROBOT_BACKEND_URL": f"http://127.0.0.1:{errand_port}",
        "ANNIE_BODY_URL": f"http://127.0.0.1:{dog_port}",
        "ANNIE_APP_URL": f"http://127.0.0.1:{app_port}",
        "ANNIE_DOG_VIEW_URL": f"http://127.0.0.1:{dog_port}",
        "ANNIE_FAMILY_MOCK_ROBOT": "false",
        # "*" (not a bracketed IPv6 list): httpx misparses '[::1]' as an invalid
        # port and then rejects every request, even to localhost. A bare star
        # bypasses all proxies, which is what this isolated stack wants.
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    return env


def port_listener_pid(port: int) -> str:
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1:
            return parts[1]
    return "unknown"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def preflight(ports, venv_py: Path, dimos_py: Path) -> list:
    errors = []
    if not venv_py.exists():
        errors.append(f"repo .venv missing: {venv_py} (run the README setup first)")
    else:
        for module in ("uvicorn", "fastapi"):
            r = subprocess.run([str(venv_py), "-c", "import " + module],
                               capture_output=True, text=True)
            if r.returncode != 0:
                errors.append(f"{module} missing from repo .venv ({venv_py}); "
                              "run the README setup first")
    if not dimos_py.exists():
        errors.append(f"simulator venv missing: {dimos_py} (see robot/simulation/README.md)")
    else:
        r = subprocess.run([str(dimos_py), "-c", "import mujoco"], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f"mujoco not importable in {dimos_py}: {r.stderr.strip()[:200]}")
    for port in ports:
        if not port_free(port):
            errors.append(f"port {port} already in use (pid {port_listener_pid(port)}); "
                          "stop that process or pass --app-port/--errand-port/--dog-port")
    for err in errors:
        print("preflight FAIL: " + err, file=sys.stderr)
    return errors


def http_get_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None


def wait_ready(check, deadline_s: float, what: str, poll_s: float = 1.0) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(poll_s)
    print(f"readiness timeout after {deadline_s:g}s: {what}", file=sys.stderr)
    return False


def tail(path: Path, lines: int = 30) -> str:
    try:
        return chr(10).join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


class Stack:
    """Child processes, each in its own session/process group; cleanup touches
    only these groups."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.procs = {}

    def spawn(self, name: str, argv, env) -> None:
        with open(self.log_dir / (name + ".log"), "ab") as log:
            self.procs[name] = subprocess.Popen(argv, cwd=str(ROOT), env=env,
                                            stdout=log, stderr=log, start_new_session=True)

    def exited(self):
        return [n for n, p in self.procs.items() if p.poll() is not None]

    def stop(self) -> None:
        for sig in (signal.SIGINT, signal.SIGKILL):
            for proc in self.procs.values():
                if proc.poll() is None:
                    with suppress(OSError):
                        os.killpg(proc.pid, sig)
            deadline = time.monotonic() + (4.0 if sig == signal.SIGINT else 2.0)
            while time.monotonic() < deadline and any(p.poll() is None for p in self.procs.values()):
                time.sleep(0.2)
        for proc in self.procs.values():
            if proc.poll() is None:
                with suppress(OSError):
                    proc.kill()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="offline full-stack launcher (hardware off)")
    parser.add_argument("--app-port", type=int, default=APP_PORT_DEFAULT)
    parser.add_argument("--errand-port", type=int, default=ERRAND_PORT_DEFAULT)
    parser.add_argument("--dog-port", type=int, default=DOG_PORT_DEFAULT)
    parser.add_argument("--sim", choices=("seated", "floor", "empty"), default="seated")
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    parser.add_argument("--sim-root", default=None, help="data/log root (default .data/sim/stack-<ts>)")
    parser.add_argument("--check", action="store_true", help="preflight only, no launch")
    args = parser.parse_args(argv)

    for label in ("app_port", "errand_port", "dog_port"):
        value = getattr(args, label)
        if not (1024 <= value <= 65535):
            parser.error(f"--{label.replace('_', '-')} must be 1024..65535, got {value}")
    if not (10.0 <= args.duration <= 86400.0):
        parser.error("--duration must be between 10 and 86400 seconds")
    if not (5.0 <= args.ready_timeout <= 600.0):
        parser.error("--ready-timeout must be between 5 and 600 seconds")
    if args.sim_root and (args.sim_root.startswith("/") or ".." in Path(args.sim_root).parts):
        parser.error("--sim-root must be a relative path inside the repository")

    venv_py = ROOT / ".venv" / "bin" / "python"
    dimos_py = ROOT / ".cache" / "dimos" / ".venv" / "bin" / "python"
    ports = [args.app_port, args.errand_port, args.dog_port]
    if len(set(ports)) != 3:
        parser.error("app, errand and dog ports must differ")
    if preflight(ports, venv_py, dimos_py):
        return 2
    if args.check:
        print("preflight OK: .venv backend deps, dimos venv + mujoco, all ports free")
        return 0

    if args.sim_root:
        sim_root = Path(args.sim_root)
        sim_root = (ROOT / sim_root).resolve() if not sim_root.is_absolute() else sim_root
    else:
        sim_root = ROOT / ".data" / "sim" / ("stack-" + time.strftime("%Y%m%d-%H%M%S"))
    sim_root.mkdir(parents=True, exist_ok=True)
    (sim_root / "faces").mkdir(exist_ok=True)
    env = build_env(app_port=args.app_port, errand_port=args.errand_port,
                    dog_port=args.dog_port, sim_root=sim_root)

    app = f"http://127.0.0.1:{args.app_port}"
    errand = f"http://127.0.0.1:{args.errand_port}"
    dog = f"http://127.0.0.1:{args.dog_port}"

    stack = Stack(sim_root / "logs")
    exit_code = 1
    prior_handlers = {}

    def on_signal(signum, frame):  # installed before the first child: no leak window
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            prior_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, on_signal)
        for name, argv_ in (
            ("app", [str(venv_py), "-m", "uvicorn", "app_backend.app.main:app",
                     "--host", "127.0.0.1", "--port", str(args.app_port), "--no-proxy-headers"]),
            ("errand", [str(venv_py), "robot/go2_errand.py", "--host", "127.0.0.1",
                        "--port", str(args.errand_port),
                        "--body-url", f"http://127.0.0.1:{args.dog_port}",
                        "--app-url", f"http://127.0.0.1:{args.app_port}"]),
            ("dog", [str(dimos_py), "robot/go2_patrol_greet.py", "--sim", args.sim,
                     "--duration", str(args.duration), "--mock-audio",
                     "--mock-transcript", "okay thank you",
                     "--view-port", str(args.dog_port),
                     "--memory-file", str(sim_root / "sightings.jsonl"),
                     "--spacetime-file", str(sim_root / "spacetime.jsonl")]),
        ):
            if stack.exited():  # an earlier child already died: stop right here
                raise RuntimeError(f"{stack.exited()[0]} exited during startup")
            stack.spawn(name, argv_, env)
            print(f"started {name:6s} pid {stack.procs[name].pid}")

        def app_ready():
            return http_get_json(app + "/health") is not None

        def errand_ready():
            return (http_get_json(errand + "/health") or {}).get("ok") is True

        def dog_ready():
            tel = http_get_json(dog + "/telemetry.json")
            return bool(tel) and tel.get("source") == "simulation" and tel.get("connected") is True

        ready = wait_ready(app_ready, args.ready_timeout, f"app {app}/health")
        ready = wait_ready(errand_ready, args.ready_timeout, "errand /health") and ready
        ready = wait_ready(dog_ready, args.ready_timeout, "sim dog source=simulation connected") and ready
        gone = stack.exited()
        if not ready or gone:
            raise RuntimeError("startup failed: " + ", ".join(gone or ["readiness timeout"]))

        (sim_root / "stack.json").write_text(json.dumps({
            "app_port": args.app_port, "errand_port": args.errand_port, "dog_port": args.dog_port,
            "pids": {n: p.pid for n, p in stack.procs.items()}, "sim_root": str(sim_root),
            "source": "simulation",
        }, indent=2))
        print(f"simulated stack up (data: {sim_root}; source=simulation for app+errand+dog)")
        print(f"  family app  {app}   loopback, no token; web UI {app}/app/")
        print(f"  errand      {errand}   dispatching to the simulated body")
        print(f"  sim dog     {dog}   source=simulation, mock audio ('okay thank you')")
        print(f"probe: .venv/bin/python robot/verify_sim.py --app-port {args.app_port} "
              f"--errand-port {args.errand_port} --dog-port {args.dog_port}")
        while True:
            gone = stack.exited()
            if gone:
                raise RuntimeError("unexpected child exit: " + ", ".join(gone))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print("stopping simulated stack...")
        exit_code = 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        gone = stack.exited()
        for name in gone:
            print(f"--- {name}.log (tail) ---", file=sys.stderr)
            print(tail(stack.log_dir / (name + ".log")), file=sys.stderr)
    finally:
        stack.stop()
        for sig, handler in prior_handlers.items():  # in-process test runs keep a clean slate
            signal.signal(sig, handler)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
