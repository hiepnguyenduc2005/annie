"""Model receives measured progress; successful calls settle actual usage."""
import json

from robot.robot_backend.tests.test_planner import client, config, plan_request, wire_reply


def test_progress_is_model_context_and_cost_settles_once(tmp_path):
    ledger = tmp_path / 'usage.json'
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return wire_reply()
    payload = plan_request()
    payload['progress'] = {'navigation_state': 'idle', 'active_waypoint': None,
        'completed_visits': [{'waypoint_id': 'kitchen', 'command_id': 'measured-arrival',
                              'completed_at': payload['observation']['ts'] - 1000}],
        'unvisited_waypoints': [], 'incident_episode_active': True,
        'recent_events': [{'event_id': 'incident-result', 'kind': 'fall_confirmed',
                           'ts': payload['observation']['ts'] - 500}]}
    with client(handle, config(usage_path=str(ledger))) as api:
        assert api.post('/plan', json=payload).status_code == 200
    text = seen[0]['messages'][1]['content'][0]['text']
    assert json.loads(text)['progress'] == payload['progress']
    state = json.loads(ledger.read_text())
    assert state['total_reserved_usd'] == .001  # Exact successful wire-reported cost.
    assert state['pending_reservations'] == {}
    assert sum(entry['attempts'] for entry in state['models'].values()) == 1


def test_unusable_response_retains_full_reservation(tmp_path):
    import httpx
    ledger = tmp_path / 'usage.json'
    with client(lambda _: httpx.Response(200, json={'usage': {'cost': 0}}),
                config(usage_path=str(ledger))) as api:
        assert api.post('/plan', json=plan_request()).status_code == 502
    assert json.loads(ledger.read_text())['total_reserved_usd'] == .12
