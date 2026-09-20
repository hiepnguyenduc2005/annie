"""Progress comes from receipts; incident completion restores model decisions."""
import asyncio
from types import SimpleNamespace

from robot.simulation.bridge import Bridge
from robot.simulation.navigation import Navigator
from robot.simulation.task_progress import task_progress


def test_only_measured_completed_arrivals_count_as_visits():
    state = {'map_id': 'home', 'navigation': {'state': 'moving', 'waypoint': 'kitchen',
        'waypoints': [{'id': 'kitchen'}, {'id': 'bedroom'}], 'commands': [
            {'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'executing', 'command_id': 'queued', 'completed_at': 80},
            {'cmd': 'goto', 'waypoint': 'bedroom', 'status': 'completed', 'command_id': 'arrived', 'completed_at': 50},
            {'cmd': 'goto', 'waypoint': 'kitchen', 'status': 'completed', 'command_id': 'future', 'completed_at': 101},
            {'cmd': 'stop', 'waypoint': 'kitchen', 'status': 'completed', 'command_id': 'stopped', 'completed_at': 60}]}}
    progress = task_progress(state, now_ms=100)
    assert progress['completed_visits'] == [{'waypoint_id': 'bedroom', 'command_id': 'arrived', 'completed_at': 50}]
    assert progress['unvisited_waypoints'] == ['kitchen']


def test_old_map_incidents_cannot_be_current_goal_feedback():
    state = {'map_id': 'new', 'navigation': {}}
    event = {'event_id': 'old', 'kind': 'fall_confirmed', 'ts': 99, 'evidence': {'pose': {'map_id': 'old'}}}
    progress = task_progress(state, events=[event], episode_active=True, now_ms=100)
    assert progress['incident_episode_active']
    assert progress['recent_events'] == []


def test_navigation_completion_has_timestamp_and_retains_destination(monkeypatch):
    monkeypatch.setattr('robot.simulation.navigation.time.time', lambda: 10.)
    active = {'cmd': 'goto', 'waypoint': 'bedroom', 'status': 'executing'}
    Navigator.update(SimpleNamespace(active=active), 'completed', 'Measured arrival')
    assert active['completed_at'] == 10000 and active['waypoint'] == 'bedroom'


def test_handled_incident_returns_to_planning_instead_of_infinite_confirmation():
    async def check(handled):
        bridge = Bridge(None, None, None, perception='agent', continuous_local=True)
        bridge.incident_episode_active = handled
        bridge.ingest_accepted = True
        bridge.last_perception = {'person': True, 'posture': 'lying', 'location': 'floor',
                                  'confidence': .9, 'pose': {'map_id': 'home'}}
        state = {'ready': True, 'map_id': 'home', 'running': True, 'intelligence_enabled': True,
                 'navigation': {'state': 'idle'}}
        calls = []
        async def request(*a, **k): return state
        async def body(*a): pass
        async def infer(): calls.append('confirmation')
        async def think(*a): calls.append('plan')
        bridge.request, bridge.body, bridge.infer, bridge.think = request, body, infer, think
        await bridge.tick(wait_vision=True)
        return calls
    assert asyncio.run(check(False)) == ['confirmation']
    assert asyncio.run(check(True)) == ['plan']
