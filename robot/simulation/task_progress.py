"""Measured execution context for the model; this module never chooses actions."""


def execution_outcomes(state):
    """Join typed speech receipts to their clips before sending planner context.

    Older viewer builds omit cmd on audio receipts. Never pass an untyped
    receipt through, and include completed speech so the model need not repeat it.
    """
    speech = {item['command_id']: item for item in state.get('speech', [])}
    outcomes = []
    for item in (state.get('navigation') or {}).get('commands', [])[-4:]:
        clip = speech.get(item.get('command_id'))
        cmd = item.get('cmd') or ('say' if clip else None)
        if not cmd: continue
        detail = ('Speech '+item['status']+': '+str(clip.get('text', ''))) if clip else item.get('detail', '')
        outcomes.append({'command_id': item['command_id'], 'cmd': cmd,
                         'status': item['status'], 'detail': detail[:300]})
    return outcomes


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
