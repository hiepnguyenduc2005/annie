from robot.simulation.person_safety import PersonSafety
from robot.simulation.viewer import validate_control
import pytest


def observation(map_id='sim-one', frame_id='frame-one', ts=10000):
    return {'pose': {'map_id': map_id}, 'frame_id': frame_id, 'ts': ts}


def result(person=False, frame_id='frame-one'):
    return {'frame_id': frame_id, 'persons': [{'confidence': .9}] if person else [],
            'stop_recommended': person, 'latency_ms': 30, 'model_path': 'test'}


def test_person_stop_latches_until_fresh_clear_and_explicit_restart():
    guard = PersonSafety(clock=lambda: 10, start=False)
    guard.reset('sim-one')
    assert guard.snapshot()['blocked']  # Cold/missing result inhibits motion.
    guard.record(observation(), result())
    assert not guard.snapshot()['blocked']
    guard.record(observation(), result(True))
    assert guard.snapshot()['blocked']
    guard.explicit_restart()
    assert guard.snapshot()['blocked']
    guard.record(observation(), result())
    assert guard.snapshot()['blocked']  # Clear frames alone cannot restart motion.
    guard.explicit_restart()
    assert not guard.snapshot()['blocked']


def test_stale_future_old_map_and_wrong_capture_cannot_enable_motion():
    guard = PersonSafety(clock=lambda: 10, start=False)
    guard.reset('sim-one')
    guard.record(observation(ts=8000), result())
    assert guard.snapshot()['blocked']
    guard.record(observation(ts=11000), result())
    assert guard.snapshot()['blocked']
    guard.reset('sim-two')
    guard.record(observation(), result())
    assert not guard.snapshot()['ready']
    guard.record(observation(map_id='sim-two'), result(frame_id='other'))
    assert guard.snapshot()['blocked']


def test_turn_contract_absolute_finite_heading():
    assert validate_control({'action': 'mission', 'cmd': 'turn', 'heading': 0})['heading'] == 0
    for heading in (None, True, float('nan'), 4, '1'):
        with pytest.raises(ValueError):
            validate_control({'action': 'mission', 'cmd': 'turn', 'heading': heading})


def test_interlock_zeros_controller_without_waiting_for_inference():
    from robot.simulation.viewer import MujocoSession
    from types import SimpleNamespace
    class Controller:
        def apply(self, *velocity):
            self.velocity = velocity
    class Navigator:
        state = 'moving'
        def fail(self, reason):
            self.reason, self.state = reason, 'failed'
        def velocity(self):
            raise AssertionError('Blocked navigator must not issue targets')
    guard = PersonSafety(clock=lambda: 10, start=False)
    guard.reset('sim-one')
    body = SimpleNamespace(model=None, data=None, controller=Controller(),
                           navigator=Navigator(), person_safety=guard)
    MujocoSession.controls(body)
    assert body.controller.velocity == (0, 0, 0)
    assert body.navigator.state == 'failed'
