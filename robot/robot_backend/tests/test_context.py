import json
from robot.robot_backend.app.brain.context import pack_context

def request():
    return {'observation':{'frame_id':'live','ts':100,'pose':{'x':0,'y':0,'yaw':0,'map_id':'home'}},
            'goal':'Check the bedroom','waypoints':[{'id':'bedroom','x':2,'y':3}],
            'recent_outcomes':[],'memories':[]}

def test_context_keeps_citations_and_rejects_cross_map_memory():
    req=request()
    req['memories']=[{'frame_id':'old','ts':50,'caption':'A chair','pose':req['observation']['pose']},
        {'frame_id':'wrong','ts':90,'caption':'Wrong house','pose':{'x':0,'y':0,'yaw':0,'map_id':'away'}}]
    text,stats=pack_context(req)
    assert json.loads(text)['memories']==req['memories'][:1]
    assert stats['memories_included']==1 and stats['memories_omitted']==1
    assert 'jpeg_b64' not in text

def test_context_has_exact_byte_bound_with_unicode_without_partial_evidence():
    req=request()
    req['memories']=[{'frame_id':str(i),'ts':i,'caption':'家具'*100,'pose':req['observation']['pose']} for i in range(6)]
    text,stats=pack_context(req,max_bytes=1700)
    assert len(text.encode())<=1700
    assert 0<stats['memories_included']<6
    for m in json.loads(text)['memories']:
        assert all(k in m for k in ('frame_id','ts','pose','caption'))

def test_goal_and_current_observation_are_never_dropped():
    req=request();req['goal']='x'*2000
    import pytest
    with pytest.raises(ValueError):pack_context(req,max_bytes=500)
