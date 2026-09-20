"""Execution boundary for model-selected actions; never chooses a mission.

The model chooses intent. This gate checks current feedback before handing a
bounded command to the existing app queue. No resident scenario label enters
this decision. Refusal is returned to the model on its next turn.
"""
import math


def gate_action(action, *, state, status, frame, now_ms, last_speech_ms=None):
    if state.get('map_id') != frame['pose']['map_id']:
        return None,'Scene changed while the model was thinking'
    if not 0 <= now_ms-frame['ts'] <= 5000:
        return None,'Model decision expired before execution'
    kind=action['action']
    if kind=='wait':
        return None,'Model chose to wait'
    if kind=='stop':
        return {'cmd':'stop'},'Model requested a stop'
    if state.get('physics_error') or not state.get('running'):
        return None,'Simulation is paused or faulted'
    if status.get('pending_checkin'):
        return None,'Incident check-in owns motion and speech until resolved'
    if kind=='finish':
        if (state.get('navigation') or {}).get('state') in ('moving','scanning','turning'):
            return None,'Cannot finish while current motion has no terminal execution receipt'
        if any(clip.get('status') in ('queued','playing','generated') for clip in state.get('speech',[])):
            return None,'Cannot finish while audio is pending or playing'
        # Completion ends planning for the operator goal; it sends no motor command.
        return None,'Model completed the goal'
    safety=state.get('person_safety') or {}
    resident_guard=state.get('resident_guard') or {}
    if kind in ('goto','look'):
        if (not safety.get('ready') or (safety.get('enforced',True) and safety.get('blocked'))
                or (resident_guard.get('enforced',True) and resident_guard.get('blocked'))):
            return None,'Person/stale-camera interlock holds motion; explicit operator restart required'
        if (state.get('navigation') or {}).get('state') in ('moving','scanning','turning'):
            return None,'Current motion has no terminal execution receipt yet'
        if kind=='look':
            return {'cmd':'look'},'Model-selected scan accepted'
        waypoint=action.get('waypoint_id')
        if waypoint not in {p['id'] for p in state['navigation']['waypoints']}:
            return None,'Model waypoint is not in the current navigable map'
        target=next(p for p in state['navigation']['waypoints'] if p['id']==waypoint)
        pose=state.get('qpos_base')
        if pose and math.hypot(pose[0]-target['x'],pose[1]-target['y'])<.3:
            return None,'Already at that waypoint; choose observation, speech, or a different destination'
        return {'cmd':'goto','waypoint':waypoint},'Model-selected waypoint accepted'
    if kind=='say':
        if last_speech_ms is not None and now_ms-last_speech_ms<15000:
            return None,'Speech cooldown; do not repeat or interrupt'
        if any(clip.get('status') in ('queued','playing','generated') for clip in state.get('speech',[])):
            return None,'Another audio clip is pending or playing'
        text=action.get('text')
        if not isinstance(text,str) or not text.strip() or len(text)>500:
            return None,'Model speech was empty or too long'
        return {'cmd':'say','text':text},'Model-selected speech accepted'
    return None,'Unknown model action'
