"""Progress comes from receipts; incident completion restores model decisions."""
import asyncio
import json
import httpx
import pytest
from types import SimpleNamespace

from robot.simulation.bridge import Bridge
from robot.simulation.navigation import Navigator
from robot.simulation.task_progress import task_progress, execution_outcomes


@pytest.mark.parametrize('accepted,pending', [(True, False), (False, False), (True, True)])
def test_model_completion_ends_calls_only_after_evidence_and_receipts(accepted, pending):
    async def check():
        bridge = Bridge(None, None, None, perception='agent', continuous_local=True, clock=lambda: 1.)
        bridge.map_id = 'home'
        pose = {'x': 0., 'y': 0., 'yaw': 0., 'map_id': 'home'}
        frame = {'frame_id': 'current-frame', 'ts': 1000, 'pose': pose,
                 'source': 'simulation_render', 'jpeg_b64': 'mocked-camera'}
        state = {'ready': True, 'map_id': 'home', 'running': True, 'intelligence_enabled': True,
                 'intelligence_revision': 7, 'intelligence_goal': 'Observe the room',
                 'navigation': {'state': 'idle', 'waypoints': [], 'commands': []}, 'speech': []}
        calls = []
        async def request(client, method, path, **kwargs):
            calls.append((method, path))
            if path in ('/query', '/recall'): return {'citations': []}
            if path == '/events': return []
            if path == '/observation': return frame
            if path == '/state': return state
            if path == '/status': return {'pending_checkin': None}
            if path == '/commands' and method == 'GET':
                return [{'cmd': 'say', 'status': 'queued'}] if pending else []
            if path == '/plan': return {'frame_id': frame['frame_id'], 'ts': frame['ts'], 'pose': pose,
                'perception': {'person': False, 'posture': 'unknown', 'location': 'unknown',
                               'confidence': .9, 'caption': 'A chair is visible.'},
                'action': {'action': 'finish', 'reason': 'Observed a chair.'},
                'provider': {'model': 'mock', 'mode': 'local'}, 'latency_ms': 10}
            raise AssertionError((method, path))
        body_updates = []
        async def body(*args): body_updates.append(True)
        async def ingest(*args): return {'accepted': accepted}
        bridge.request, bridge.ingest, bridge.body = request, ingest, body
        await bridge.think(state)
        assert bool(bridge.goal_completion) == (accepted and not pending)
        assert ('POST', '/commands') not in calls and ('POST', '/say') not in calls
        if bridge.goal_completion:
            assert bridge.agent_state['goal_status'] == 'completed'
            bridge.next_inference = 0
            await bridge.tick(wait_vision=True)
            assert body_updates and calls.count(('POST', '/plan')) == 1
            # A new operator goal explicitly resumes planning; no timer does.
            state['intelligence_revision'] = 8
            state['intelligence_goal'] = 'Observe again'
            frame['frame_id'] = 'new-frame'
            await bridge.tick(wait_vision=True)
            assert calls.count(('POST', '/plan')) == 2
    asyncio.run(check())


def test_memory_retrieval_uses_operator_goal_and_preserves_relevant_citation():
    async def check():
        requests = []
        cited = {'frame_id': 'glasses', 'ts': 900, 'caption': 'Glasses on a table',
                 'pose': {'x': 0., 'y': 0., 'yaw': 0., 'map_id': 'home'}}
        def handler(request):
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={'citations': [cited] if request.url.path == '/recall' else [],
                                            'provider': 'lexical_fallback'})
        async with httpx.AsyncClient(base_url='http://app', transport=httpx.MockTransport(handler)) as app:
            bridge = Bridge(None, app, None, clock=lambda: 1.)
            assert await bridge.retrieve_memories('Find the glasses', 'home') == [cited]
            assert next(body for path, body in requests if path == '/recall')['goal'] == 'Find the glasses'
            assert bridge.memory_state['retrieval'] == 'lexical_fallback'
    asyncio.run(check())


def test_positive_sighting_survives_empty_retrieval_and_preserves_capture_identity():
    async def check():
        positive = {'frame_id': 'seen', 'ts': 500, 'caption': 'A human stands nearby',
                    'pose': {'x': 2., 'y': 4., 'yaw': 1., 'map_id': 'home'}}
        empty = {**positive, 'frame_id': 'empty', 'ts': 900, 'caption': 'Empty room'}
        replies = [[positive], []]
        def handler(request):
            is_person = json.loads(request.content).get('text') == 'where was the person last seen'
            return httpx.Response(200, json={'citations': replies.pop(0) if is_person else [empty]})
        async with httpx.AsyncClient(base_url='http://app', transport=httpx.MockTransport(handler)) as app:
            bridge = Bridge(None, app, None, clock=lambda: 1.)
            await bridge.retrieve_memories('Find resident', 'home')
            await bridge.retrieve_memories('Find resident', 'home')
            assert bridge.last_person_sighting == positive
            # The small rolling observation window can be full of empty views.
            bridge.agent_memory = [empty] * 6
            for bad in ({**positive, 'ts': 499}, {**positive, 'ts': 1001},
                        {**positive, 'pose': {**positive['pose'], 'map_id': 'old'}}):
                bridge.remember_person(bad, 'home')
            assert bridge.last_person_sighting == positive
            progress = task_progress({'map_id': 'home'}, last_person_sighting=bridge.last_person_sighting, now_ms=1000)
            from robot.robot_backend.app.brain.planner import TaskProgress
            assert TaskProgress.model_validate(progress).last_person_sighting.model_dump() == positive
            assert task_progress({'map_id': 'new'}, last_person_sighting=positive, now_ms=1000)['last_person_sighting'] is None
            assert task_progress({'map_id': 'home'}, last_person_sighting=positive, now_ms=400)['last_person_sighting'] is None
    asyncio.run(check())


def test_packed_context_excludes_cross_map_and_future_sighting_without_mutating_source():
    from robot.robot_backend.app.brain.context import pack_context
    pose = {'x': 0., 'y': 0., 'yaw': 0., 'map_id': 'home'}
    citation = {'frame_id': 'seen', 'ts': 500, 'caption': 'Person visible', 'pose': pose}
    request = {'goal': 'Find resident', 'observation': {'frame_id': 'now', 'ts': 1000, 'pose': pose},
               'progress': {'last_person_sighting': citation}}
    assert json.loads(pack_context(request)[0])['progress']['last_person_sighting'] == citation
    for invalid in ({**citation, 'ts': 1001}, {**citation, 'pose': {**pose, 'map_id': 'old'}}):
        request['progress']['last_person_sighting'] = invalid
        assert json.loads(pack_context(request)[0])['progress']['last_person_sighting'] is None
        assert request['progress']['last_person_sighting'] == invalid


def test_completed_speech_without_cmd_still_produces_valid_planner_feedback():
    from robot.robot_backend.app.brain.planner import RecentOutcome
    state = {'navigation': {'commands': [
        {'command_id': 'audio', 'status': 'completed'},
        {'command_id': 'unknown', 'status': 'completed'}]},
        'speech': [{'command_id': 'audio', 'text': 'A person is visible.'}]}
    results = execution_outcomes(state)
    assert len(results) == 1
    parsed = RecentOutcome.model_validate(results[0])
    assert parsed.cmd == 'say' and 'A person is visible.' in parsed.detail


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
