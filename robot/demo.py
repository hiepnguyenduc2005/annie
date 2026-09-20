"""Start the local Annie simulator stack; Ctrl-C stops all owned processes."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PORTS = (8000, 8766, 8004)
APP = 'http://127.0.0.1:8000'
VIEWER = 'http://127.0.0.1:8766'
BRAIN = 'http://127.0.0.1:8004'
STATUS_FILE = ROOT / '.data/simulation/bridge-status.json'


def occupied_ports():
    """Identify listeners first; never signal an existing service."""
    occupied = []
    for port in PORTS:
        result = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fp'],
                                text=True, capture_output=True, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError(f'Cannot inspect port {port} with lsof')
        pids = sorted({line[1:] for line in result.stdout.splitlines() if line.startswith('p')})
        if pids:
            occupied.append(f':{port} PID {", ".join(pids)}')
    if occupied:
        return occupied
    # Catch a conflicting bind that lsof could not attribute to this user.
    for port in PORTS:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as exc:
                raise RuntimeError(f'Cannot bind port {port}: {exc}; listener PID unavailable') from exc
    return []


def launch_commands():
    return {
        'app': ['.venv/bin/uvicorn', 'robot.app_backend.app.main:app', '--host', '127.0.0.1',
                '--port', '8000', '--no-proxy-headers'],
        'viewer': ['.cache/dimos/.venv/bin/python', 'robot/simulation/viewer.py',
                   '--model', '.cache/menagerie/unitree_go2/scene.xml',
                   '--scenes', '.data/simulation/scenes/manifest.json', '--locomotion',
                   '--person-safety', '--person-policy', 'advisory', '--native-audio',
                   '--demo-brain-url', BRAIN, '--port', '8766'],
        'brain': ['.venv/bin/python', 'robot/simulation/run_brain.py', '--mode', 'local', '--port', '8004'],
        'bridge': ['.venv/bin/python', 'robot/simulation/bridge.py', '--perception', 'agent',
                   '--brain-url', BRAIN, '--continuous-local', '--status-file', str(STATUS_FILE)],
    }


class Demo:
    def __init__(self, *, timeout=90, poll=.25, env=None, output=print):
        self.timeout, self.poll, self.output = timeout, poll, output
        self.env = dict(os.environ if env is None else env)
        self.env.update(ANNIE_MODE='demo', ANNIE_REQUIRE_AUDIO_RECEIPT='true',
                        ANNIE_MEMORY_PROVIDER='none', PYTHONUNBUFFERED='1')
        self.processes = []
        self.logs = ExitStack()
        self.log_dir = ROOT / '.data/simulation/demo-logs' / time.strftime('%Y%m%d-%H%M%S')
        self.opener = build_opener(ProxyHandler({}))

    def request(self, url, payload=None):
        headers = {}
        token = self.env.get('ANNIE_API_TOKEN')
        if url.startswith(BRAIN + '/'):
            token = self.env.get('ANNIE_BRAIN_TOKEN') or token
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        with self.opener.open(Request(url, data=data, headers=headers), timeout=3) as response:
            return json.load(response)

    def check_children(self):
        for name, child in self.processes:
            code = child.poll()
            if code is not None:
                raise RuntimeError(f'{name} exited ({code}); see {self.log_dir / (name + ".log")}')

    def start(self, name):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        logfile = self.logs.enter_context((self.log_dir / (name + '.log')).open('w'))
        child = subprocess.Popen(launch_commands()[name], cwd=ROOT, env=self.env,
                                 stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((name, child))
        self.output(f'{name}: PID {child.pid}', flush=True)

    def wait(self, label, predicate):
        started = time.monotonic()
        last_error = None
        while time.monotonic() - started < self.timeout:
            self.check_children()
            try:
                result = predicate()
                if result:
                    self.output(f'{label}: ready in {time.monotonic()-started:.3f} s', flush=True)
                    return result
            except (URLError, OSError, ValueError, KeyError) as exc:
                last_error = type(exc).__name__
            time.sleep(self.poll)
        raise TimeoutError(f'{label} not ready after {self.timeout:g} s (last error: {last_error}); logs: {self.log_dir}')

    def start_stack(self):
        occupied = occupied_ports()
        if occupied:
            raise RuntimeError('Refusing occupied ports: ' + '; '.join(occupied))
        self.start('app')
        self.wait('app /health', lambda: self.request(APP + '/health'))
        self.start('viewer')
        self.wait('viewer /state', lambda: self.request(VIEWER + '/state').get('ready'))
        initial = self.request(VIEWER + '/state')
        if (initial.get('current_scene') or {}).get('id') != 'grandmas-house':
            self.request(VIEWER + '/control', {'action': 'scene', 'id': 'grandmas-house'})
        def house_ready():
            state = self.request(VIEWER + '/state')
            return (state if state.get('ready') and not state.get('physics_error')
                    and (state.get('current_scene') or {}).get('id') == 'grandmas-house'
                    and state.get('navigation') else None)
        state = self.wait('grandmas-house locomotion', house_ready)
        map_id = state['map_id']
        self.request(VIEWER + '/control', {'action': 'play'})
        def detector_ready():
            current = self.request(VIEWER + '/state')
            return (current.get('running') and not current.get('physics_error')
                    and (current.get('person_safety') or {}).get('ready'))
        self.wait('viewer playback and person detector', detector_ready)
        self.start('brain')
        def local_brain():
            health = self.request(BRAIN + '/health')
            if health.get('mode') != 'local':
                raise RuntimeError('Brain must be in local mode')
            return health
        self.wait('brain /health (local)', local_brain)
        bridge_started = int(time.time()*1000)
        self.start('bridge')
        def bridge_ready():
            status = json.loads(STATUS_FILE.read_text())
            return (status.get('updated_at', 0) >= bridge_started
                    and status.get('context_map_id') == map_id
                    and status.get('perception_mode') == 'agent'
                    and status.get('continuous_local') is True)
        self.wait('bridge status', bridge_ready)
        self.output(f'Family app: {APP}/app/\nSimulator:  {VIEWER}/\nLogs: {self.log_dir}\nCtrl-C stops this stack.', flush=True)

    def close(self):
        # Each child owns a new process group. Signal groups even if a leader exited,
        # so its remaining descendants do not survive a startup failure.
        for sig, grace in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (signal.SIGKILL, 1)):
            for _, child in reversed(self.processes):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + grace
            for _, child in reversed(self.processes):
                try:
                    child.wait(timeout=max(.01, deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            # All three signals also clean up descendants of exited parents.
        self.logs.close()
        self.processes.clear()

    def run(self):
        try:
            self.start_stack()
            while True:
                self.check_children()
                time.sleep(self.poll)
        finally:
            self.close()


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=positive, default=90, help='Readiness deadline per service, seconds')
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
    demo = Demo(timeout=args.timeout)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        demo.run()
    except KeyboardInterrupt:
        print('Demo stack stopped.')
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Demo startup/run failed: {exc}', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
