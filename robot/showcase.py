"""Timed HTTP showcases for the local MuJoCo stack started by robot/demo.py."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
TRICKS = ('spin', 'circle', 'zigzag', 'wiggle', 'figure8')
TERMINAL = {'completed', 'failed'}


class Showcase:
    def __init__(self, app, viewer, brain, *, timeout=180, poll=.25,
                 status_file=ROOT / '.data/simulation/bridge-status.json',
                 clock=time.monotonic, sleep=time.sleep, output=print):
        self.app, self.viewer, self.brain = app, viewer, brain
        self.timeout, self.poll = timeout, poll
        self.status_file = Path(status_file)
        self.clock, self.sleep, self.output = clock, sleep, output

    def request(self, client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    @contextmanager
    def timed(self, label):
        started = self.clock()
        try:
            yield
        finally:
            self.output(f'{label}: {self.clock() - started:.3f} s')

    def wait(self, label, predicate):
        deadline = self.clock() + self.timeout
        while self.clock() < deadline:
            result = predicate()
            if result:
                return result
            self.sleep(self.poll)
        raise TimeoutError(f'{label} timed out after {self.timeout:g} s')

    def control(self, **payload):
        return self.request(self.viewer, 'POST', '/control', json=payload)

    def prepare(self):
        with self.timed('prepare simulator'):
            state = self.request(self.viewer, 'GET', '/state')
            if not state.get('ready') or state.get('physics_error') or not state.get('navigation'):
                raise RuntimeError('Viewer must be ready with locomotion enabled')
            status = self.request(self.app, 'GET', '/status')
            if status.get('pending_checkin'):
                raise RuntimeError('Resolve the active check-in before another showcase')
            self.control(action='intelligence', enabled=False,
                         goal=state.get('intelligence_goal') or 'Monitor the resident.')
            self.control(action='autonomy', mode='paused')
            self.control(action='play')
            def paused():
                current = self.request(self.viewer, 'GET', '/state')
                return (current.get('running') and not current.get('intelligence_enabled')
                        and current.get('autonomy_mode') == 'paused')
            self.wait('pause autonomous planning', paused)
        # An acknowledged stop settles any previous mission before this sequence.
        self.command({'cmd': 'stop'}, 'settle previous motion')

    def command(self, payload, label):
        with self.timed(label):
            queued = self.request(self.app, 'POST', '/commands', json=payload)
            cid = queued['command_id']
            self.output(f'{label}: queued {cid}')
            def terminal():
                commands = self.request(self.app, 'GET', '/commands')
                return next((item for item in commands if item['command_id'] == cid
                             and item['status'] in TERMINAL), None)
            receipt = self.wait(f'{label} receipt {cid}', terminal)
            self.output(f'{label}: {receipt["status"]} ({receipt.get("detail", "")})')
            if receipt['status'] != 'completed':
                raise RuntimeError(f'{label} failed; sequence stopped')
            return receipt

    def patrol(self):
        self.prepare()
        for waypoint in ('living-room', 'bedroom', 'hallway', 'home'):
            self.command({'cmd': 'goto', 'waypoint': waypoint}, f'goto {waypoint}')

    def tricks(self):
        self.prepare()
        for trick in TRICKS:
            try:
                self.command({'cmd': 'trick', 'trick': trick}, f'trick {trick}')
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422:
                    raise
                self.output(f'{trick}: trick command not available yet')

    def local_bridge(self):
        try:
            bridge = json.loads(self.status_file.read_text())
            age = (time.time()*1000 - bridge['updated_at']) / 1000
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Cannot verify the local bridge; start robot/demo.py') from exc
        if (not 0 <= age <= 5 or bridge.get('perception_mode') != 'agent'
                or not bridge.get('continuous_local')
                or (bridge.get('last_provider') or {}).get('mode', 'local') != 'local'):
            raise RuntimeError('Fall requires a fresh agent bridge with --continuous-local; start robot/demo.py')

    def fall(self):
        # No paid provider fallback, including when attaching to an existing stack.
        with self.timed('check local brain'):
            health = self.request(self.brain, 'GET', '/health')
            if health.get('mode') != 'local':
                raise RuntimeError('Fall showcase requires a local brain')
            self.local_bridge()
        self.prepare()
        state = self.request(self.viewer, 'GET', '/state')
        map_id = state['map_id']
        if not state.get('resident'):
            raise RuntimeError('Load grandmas-house with an animated resident first')
        with self.timed('reset resolved episode'):
            self.request(self.app, 'POST', '/demo/reset-episode')
        baseline = {e['event_id'] for e in self.request(self.app, 'GET', '/events')}
        with self.timed('stage resident fall'):
            self.control(action='resident', cmd='fall')
            self.control(action='intelligence', enabled=True,
                         goal='Find and check on the resident. Observe carefully and let the incident check-in finish.')
        started = self.clock()
        seen, incident, say_id, evidence = set(), None, None, None
        last_step = started
        def record(kind, detail):
            nonlocal last_step
            if kind not in seen:
                now = self.clock()
                self.output(f'+{now-started:.3f} s {kind}: {detail} (step {now-last_step:.3f} s)')
                seen.add(kind)
                last_step = now
        def timeline():
            nonlocal incident, say_id, evidence
            current = self.request(self.viewer, 'GET', '/state')
            if current.get('map_id') != map_id or current.get('physics_error'):
                raise RuntimeError('Scene changed or physics faulted during fall showcase')
            status = self.request(self.app, 'GET', '/status')
            events = [e for e in self.request(self.app, 'GET', '/events') if e['event_id'] not in baseline]
            if incident is None:
                suspected = next((e for e in events if e['kind'] == 'fall_suspected'
                                  and e.get('evidence', {}).get('pose', {}).get('map_id') == map_id), None)
                if suspected:
                    incident, evidence = suspected['event_id'], suspected['evidence']
                    record('fall_suspected', f'event={incident} ts={suspected["ts"]}')
            pending = status.get('pending_checkin')
            if incident and pending and pending['event_id'] == incident:
                say_id = pending.get('say_command_id') or say_id
                if not say_id:
                    raise RuntimeError('Restart app with ANNIE_REQUIRE_AUDIO_RECEIPT=true')
            commands = self.request(self.app, 'GET', '/commands') if say_id else []
            say = next((c for c in commands if c['command_id'] == say_id), None)
            if say:
                record('say', f'command={say_id} status={say["status"]}')
            if pending and pending.get('event_id') == incident and pending.get('phase') == 'awaiting_reply':
                if not say or say['status'] != 'completed':
                    return False
                record('reply window', f'playback completed; deadline_at={pending["deadline_at"]}')
            related = [e for e in events if evidence is not None and e.get('evidence') == evidence]
            for event in related:
                if event['kind'] in ('checkin_audio_failed', 'checkin_no_reply'):
                    record(event['kind'], event.get('reason') or 'no accepted reply')
                if event['kind'] == 'checkin_ok':
                    raise RuntimeError('Resident reassured; escalation did not occur')
                if event['kind'] == 'fall_confirmed':
                    record('escalation', f'event={event["event_id"]} ts={event["ts"]}; family attention requested')
                    if not {'fall_suspected', 'say', 'reply window'} <= seen:
                        raise RuntimeError('Escalated without observing the full playback/reply timeline')
                    return True
            return False
        try:
            with self.timed('incident timeline'):
                self.wait('fall incident timeline (inspect showcase status for inference errors)', timeline)
        finally:
            # Pause this goal on success, timeout, failure or Ctrl-C; bridge still delivers speech.
            self.control(action='intelligence', enabled=False, goal='Fall showcase finished; planning paused.')

    def status(self):
        failed = False
        for label, client, path in (('app', self.app, '/status'), ('viewer', self.viewer, '/state'),
                                    ('brain', self.brain, '/health')):
            with self.timed(label):
                try:
                    data = self.request(client, 'GET', path)
                    if label == 'app':
                        data = {k: data.get(k) for k in ('mode', 'dog', 'pending_checkin', 'incident_episode_active', 'integrations')}
                    if label == 'viewer':
                        nav = data.get('navigation') or {}
                        data = {**{k: data.get(k) for k in ('ready', 'running', 'map_id', 'physics_error', 'intelligence_enabled')},
                                'navigation': {k: nav.get(k) for k in ('state', 'waypoint', 'error')}}
                    self.output(f'{label}: {json.dumps(data, separators=(",", ":"))}')
                except (httpx.HTTPError, ValueError) as exc:
                    failed = True
                    self.output(f'{label}: unavailable ({type(exc).__name__})')
        with self.timed('bridge status file'):
            try:
                data = json.loads(self.status_file.read_text())
                summary = {k: data.get(k) for k in ('updated_at', 'perception_mode', 'context_map_id', 'inferences',
                           'inference_limit_reached', 'last_latency_ms', 'last_error', 'ingest_accepted')}
                summary['age_s'] = round((time.time()*1000 - data['updated_at']) / 1000, 2)
                summary['stale'] = summary['age_s'] > 5 or summary['age_s'] < 0
                self.output(f'bridge: {json.dumps(summary, separators=(",", ":"))}')
                failed |= summary['stale']
            except (OSError, ValueError, KeyError, TypeError):
                failed = True
                self.output(f'bridge: status unavailable ({self.status_file})')
        if failed:
            raise RuntimeError('One or more stack components unavailable or stale')


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return result


def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('showcase', choices=('patrol', 'fall', 'tricks', 'status'))
    parser.add_argument('--timeout', type=positive, default=180, help='Seconds per terminal receipt or incident timeline')
    parser.add_argument('--poll', type=positive, default=.25)
    parser.add_argument('--app-url', default='http://127.0.0.1:8000')
    parser.add_argument('--viewer-url', default='http://127.0.0.1:8766')
    parser.add_argument('--brain-url', default='http://127.0.0.1:8004')
    parser.add_argument('--status-file', type=Path, default=ROOT / '.data/simulation/bridge-status.json')
    args = parser.parse_args(argv)
    token = os.getenv('ANNIE_API_TOKEN')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with httpx.Client(base_url=args.app_url, headers=headers, timeout=5, trust_env=False) as app, \
            httpx.Client(base_url=args.viewer_url, timeout=5, trust_env=False) as viewer, \
            httpx.Client(base_url=args.brain_url, timeout=5, trust_env=False) as brain:
        runner = Showcase(app, viewer, brain, timeout=args.timeout, poll=args.poll, status_file=args.status_file)
        try:
            with runner.timed(args.showcase + ' total'):
                getattr(runner, args.showcase)()
        except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f'Showcase failed: {exc}', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print('Showcase interrupted; use demo launcher Ctrl-C to stop the stack.', file=sys.stderr)
            return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
