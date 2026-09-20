"""Launcher lifecycle tests without launching services or opening sockets."""
import json
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robot import demo


def test_occupied_ports_report_all_listener_pids_without_binding(monkeypatch):
    monkeypatch.setattr(demo.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='p42\np99\np42\n'))
    monkeypatch.setattr(demo.socket, 'socket', lambda: pytest.fail('Do not bind occupied ports'))
    assert demo.occupied_ports() == [f':{p} PID 42, 99' for p in demo.PORTS]


def test_refusal_starts_no_process_and_still_closes(monkeypatch):
    monkeypatch.setattr(demo, 'occupied_ports', lambda: [':8000 PID 42'])
    stack = demo.Demo()
    stack.start = Mock()
    stack.close = Mock()
    with pytest.raises(RuntimeError, match='8000 PID 42'):
        stack.run()
    stack.start.assert_not_called()
    stack.close.assert_called_once()


def test_stack_waits_for_health_scene_local_brain_and_fresh_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(demo, 'occupied_ports', lambda: [])
    status_file = tmp_path / 'bridge-status.json'
    monkeypatch.setattr(demo, 'STATUS_FILE', status_file)
    stack = demo.Demo(output=lambda *a, **kw: None)
    calls = []
    state = {'ready': True, 'map_id': 'sim-one', 'current_scene': {'id': 'other'},
             'navigation': {'state': 'idle'}, 'running': True, 'person_safety': {'ready': True}}
    def start(name):
        calls.append(('start', name))
        if name == 'bridge':
            status_file.write_text(json.dumps({'updated_at': int(demo.time.time()*1000),
                'context_map_id': 'sim-one', 'continuous_local': True, 'perception_mode': 'agent'}))
    def request(url, payload=None):
        calls.append((url, payload))
        if url.endswith('/state'):
            return dict(state)
        if payload and payload.get('action') == 'scene':
            state['current_scene'] = {'id': payload['id']}
        if url == demo.BRAIN + '/health':
            return {'mode': 'local'}
        return {'ok': True}
    stack.start, stack.request = start, request
    stack.start_stack()
    starts = [item[1] for item in calls if item[0] == 'start']
    assert starts == ['app', 'viewer', 'brain', 'bridge']
    assert calls.index((demo.APP+'/health', None)) < calls.index(('start', 'viewer'))
    assert calls.index((demo.BRAIN+'/health', None)) < calls.index(('start', 'bridge'))
    assert (demo.VIEWER+'/control', {'action': 'scene', 'id': 'grandmas-house'}) in calls
    assert stack.env['ANNIE_REQUIRE_AUDIO_RECEIPT'] == 'true'
    assert stack.env['ANNIE_MEMORY_PROVIDER'] == 'none'


def test_wait_checks_process_exit_instead_of_hanging():
    stack = demo.Demo()
    stack.processes = [('brain', SimpleNamespace(poll=lambda: 7))]
    with pytest.raises(RuntimeError, match=r'brain exited \(7\)'):
        stack.wait('health', lambda: False)


def test_wait_times_out_on_stale_readiness(monkeypatch):
    now = [0.]
    monkeypatch.setattr(demo.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(demo.time, 'sleep', lambda seconds: now.__setitem__(0, now[0]+seconds))
    stack = demo.Demo(timeout=1, poll=.25)
    with pytest.raises(TimeoutError, match='not ready after 1 s'):
        stack.wait('bridge', lambda: False)
    assert now[0] == 1


@pytest.mark.parametrize('failure', [KeyboardInterrupt(), RuntimeError('startup failed')])
def test_run_cleans_up_on_interrupt_and_startup_failure(failure):
    stack = demo.Demo()
    stack.start_stack = Mock(side_effect=failure)
    stack.close = Mock()
    with pytest.raises(type(failure)):
        stack.run()
    stack.close.assert_called_once()


def test_close_signals_only_owned_groups_in_reverse_order(monkeypatch):
    signals = []
    monkeypatch.setattr(demo.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    child1 = SimpleNamespace(pid=10, wait=Mock(return_value=0))
    child2 = SimpleNamespace(pid=20, wait=Mock(side_effect=subprocess.TimeoutExpired('viewer', 1)))
    stack = demo.Demo()
    stack.processes = [('app', child1), ('viewer', child2)]
    stack.close()
    assert signals == [(pid, sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL) for pid in (20, 10)]
    assert stack.processes == []


def test_launch_commands_use_documented_local_flags_and_isolated_groups(monkeypatch, tmp_path):
    stack = demo.Demo(env={}, output=lambda *a, **kw: None)
    stack.log_dir = tmp_path
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(demo.subprocess, 'Popen', spawn)
    stack.start('brain')
    argv = spawn.call_args.args[0]
    assert argv == ['.venv/bin/python', 'robot/simulation/run_brain.py', '--mode', 'local', '--port', '8004']
    assert spawn.call_args.kwargs['start_new_session'] is True
    assert spawn.call_args.kwargs['cwd'] == demo.ROOT
    commands = demo.launch_commands()
    assert '--continuous-local' in commands['bridge']
    assert '--native-audio' in commands['viewer']
    assert '--demo-allow-cloud' not in commands['viewer']
    stack.logs.close()
