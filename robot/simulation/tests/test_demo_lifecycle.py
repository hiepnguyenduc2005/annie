"""No live inference or subprocesses: exercise rehearsal ownership and timeouts."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from robot.simulation import house_demo, viewer
from robot.simulation.bridge_lease import body_bridge_lease


class Process:
    def __init__(self):
        self.code = None
    def poll(self):
        return self.code
    def terminate(self):
        self.code = -15
    def wait(self, timeout):
        return self.code


@pytest.fixture(autouse=True)
def no_live_checkin_requests(monkeypatch):
    monkeypatch.setattr(viewer, "active_checkin", lambda: False)


def request(shared, path, body=None):
    handler = viewer.handler_for(shared, 8766).__new__(viewer.handler_for(shared, 8766))
    handler.path = path
    handler.allowed = lambda **kwargs: True
    payload = json.dumps(body or {}).encode()
    handler.headers = SimpleNamespace(
        get=lambda k, default=None: {'Content-Length': str(len(payload))}.get(k, default),
        get_content_type=lambda: 'application/json')
    handler.connection = SimpleNamespace(settimeout=lambda _: None)
    handler.rfile = io.BytesIO(payload)
    result = {}
    handler.reply = lambda status, value, *args: result.update(status=status, body=value)
    handler.do_POST()
    return result


def test_demo_hands_off_its_owned_bridge_and_preserves_explicit_cloud_choice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shared = viewer.Shared()
    shared.agent_process = old = Process()
    shared.demo_allow_cloud = True
    shared.demo_brain_url = 'http://127.0.0.1:8003'
    launches = []
    def launch(argv, **kwargs):
        launches.append(argv)
        return Process()
    monkeypatch.setattr('subprocess.Popen', launch)
    assert request(shared, '/demo/start')['status'] == 202
    assert old.poll() == -15
    assert '--allow-cloud' in launches[0]
    assert 'http://127.0.0.1:8003' in launches[0]
    assert request(shared, '/demo/start')['status'] == 409
    assert len(launches) == 1


def test_existing_external_bridge_is_not_killed_or_duplicated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: pytest.fail('must not launch'))
    with body_bridge_lease():
        result = request(viewer.Shared(), '/demo/start')
    assert result['status'] == 409


def test_ai_start_starts_one_bounded_cloud_bridge_and_pause_preserves_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shared = viewer.Shared()
    shared.catalog = SimpleNamespace(entries={})
    shared.demo_allow_cloud = True
    launches = []
    def launch(argv, **kwargs):
        launches.append(argv)
        return Process()
    monkeypatch.setattr('subprocess.Popen', launch)
    goal = {'action': 'intelligence', 'enabled': True, 'goal': 'Check on the resident'}
    assert request(shared, '/agent/start', goal)['status'] == 202
    assert request(shared, '/agent/start', goal)['status'] == 202
    assert len(launches) == 1 and '--continuous-local' not in launches[0]
    assert launches[0][-2:] == ['--max-inferences', '20']
    assert request(shared, '/control', {**goal, 'enabled': False})['status'] == 202
    assert shared.agent_process.poll() is None


@pytest.mark.parametrize('value', [None, 1, 'true', []])
def test_speech_requirement_is_an_explicit_boolean(value):
    with pytest.raises(ValueError):
        viewer.validate_control({'action':'intelligence', 'enabled':True,
                                 'goal':'Deliver a message', 'require_speech':value})


def test_speech_required_control_is_compatible_with_existing_goals():
    command={'action':'intelligence', 'enabled':True, 'goal':'Deliver a message'}
    assert viewer.validate_control(command)==command
    assert viewer.validate_control({**command, 'require_speech':True})['require_speech'] is True


def test_routine_pause_fails_instead_of_hanging(monkeypatch):
    ticks = iter([0., 0., 2.])
    monkeypatch.setattr(house_demo, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    async def state(*args):
        return SimpleNamespace(json=lambda: {'simulation_time': 1., 'running': False})
    with pytest.raises(RuntimeError, match='paused'):
        asyncio.run(house_demo.wait_for_routine(SimpleNamespace(get=state), None, lambda: None))


def test_new_demo_cannot_interrupt_active_checkin(monkeypatch):
    shared=viewer.Shared()
    shared.agent_process=Process()
    monkeypatch.setattr(viewer,'active_checkin',lambda:True)
    assert request(shared,'/demo/start')['status']==409
    assert shared.agent_process.poll() is None
