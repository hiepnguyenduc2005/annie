"""Bound the working context independently of durable memory size.

Text estimates use UTF-8 bytes / 3, not the provider's tokenizer. The byte cap
is exact; image tokens and actual billed tokens come from provider usage.
Entire memories are dropped, so frame/time/pose citations are never detached.
"""
import json
import math

CONTEXT_BYTES=12000


def pack_context(request, *, max_bytes=CONTEXT_BYTES):
    data=request.model_dump(mode='json') if hasattr(request,'model_dump') else request
    frame=data['observation']
    context={'goal':data['goal'],
             'current_observation':{k:frame[k] for k in ('frame_id','ts','pose')},
             'admissible_waypoints':data.get('waypoints',[]),
             'execution_feedback':data.get('recent_outcomes',[]),
             'memories':[]}
    waypoints=data.get('waypoints',[])
    if waypoints:
        nearest=min(waypoints,key=lambda p:math.hypot(p['x']-frame['pose']['x'],p['y']-frame['pose']['y']))
        context['nearest_waypoint']={'id':nearest['id'], 'distance_m':round(math.hypot(
            nearest['x']-frame['pose']['x'],nearest['y']-frame['pose']['y']),2)}
    def encode():
        return json.dumps(context,ensure_ascii=False,separators=(',',':'))
    text=encode()
    if len(text.encode('utf-8'))>max_bytes:
        # Preserve the goal, current observation and navigation contract. Old
        # outcomes are expendable; don't silently truncate current authority.
        while context['execution_feedback'] and len(text.encode('utf-8'))>max_bytes:
            context['execution_feedback'].pop(0)
            text=encode()
        if len(text.encode('utf-8'))>max_bytes:
            raise ValueError('Goal and map exceed the working context byte budget')
    seen=set();included=0;memories=data.get('memories',[])
    for item in sorted(memories,key=lambda m:m['ts'],reverse=True):
        if item['frame_id'] in seen or item['pose']['map_id']!=frame['pose']['map_id']:
            continue
        seen.add(item['frame_id'])
        context['memories'].append(item)
        candidate=encode()
        if len(candidate.encode('utf-8'))>max_bytes:
            context['memories'].pop()
            continue
        included+=1;text=candidate
    return text,{'context_bytes':len(text.encode('utf-8')),'byte_limit':max_bytes,
                 'estimated_text_tokens':math.ceil(len(text.encode('utf-8'))/3),
                 'estimate_method':'UTF-8 bytes / 3; excludes image and system prompt',
                 'memories_included':included,'memories_omitted':len(memories)-included,
                 'output_token_limit':512}
