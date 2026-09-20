"""Measured execution context for the model; this module never chooses actions."""


def task_progress(state, *, episode_active=False, events=(), now_ms):
    nav = state.get('navigation') or {}
    known = {w['id'] for w in nav.get('waypoints', [])}
    visits = {}
    for item in nav.get('commands', []):
        waypoint, completed = item.get('waypoint'), item.get('completed_at')
        if (item.get('cmd') == 'goto' and item.get('status') == 'completed'
                and waypoint in known and type(completed) is int and 0 <= completed <= now_ms):
            visits[waypoint] = {'waypoint_id': waypoint, 'completed_at': completed,
                                'command_id': item['command_id']}
    incident_events = []
    for event in events:
        pose = (event.get('evidence') or {}).get('pose') or {}
        if pose.get('map_id') == state.get('map_id') and 0 <= event.get('ts', -1) <= now_ms:
            incident_events.append({k: event[k] for k in ('event_id', 'kind', 'ts')})
    return {'navigation_state': nav.get('state', 'unknown'),
            'active_waypoint': nav.get('waypoint'),
            'completed_visits': list(visits.values())[-30:],
            'unvisited_waypoints': sorted(known - visits.keys()),
            'incident_episode_active': bool(episode_active),
            'recent_events': incident_events[-6:]}
