"""Speech delivery survives bounded histories, without live services or inference."""
import asyncio
from uuid import uuid4

import pytest

from robot.simulation.bridge import Bridge
from robot.simulation.goal_completion import IssuedSpeech, SpeechReceiptCache, goal_speech_completion


def command(cid='spoken', status='completed', cmd='say'):
    return {'command_id':cid, 'status':status, 'cmd':cmd}


def evaluate(cache, commands=(), clips=(), *, map_id='home', revision=1, ids=('spoken',)):
    issued=[IssuedSpeech(cid,map_id,revision) for cid in ids]
    merged=cache.merge(issued,commands,clips,map_id=map_id,goal_revision=revision)
    return goal_speech_completion(issued,merged,clips,map_id=map_id,
                                  goal_revision=revision,require_speech=True)


def test_observed_completion_survives_both_histories_being_evicted():
    cache=SpeechReceiptCache()
    assert evaluate(cache,[command()],[command(status='played')]).delivery_verified
    newer=[command(str(uuid4()),cmd='stop') for _ in range(100)]
    assert evaluate(cache,newer).delivery_verified
    assert list(cache.receipts)==['spoken']


@pytest.mark.parametrize('status,cmd', [
    ('queued','say'),('executing','say'),('unknown','say'),(None,'say'),
    ('completed','goto'),('failed','say'),
])
def test_latest_conflicting_snapshot_cannot_be_masked_or_resurrect_success(status,cmd):
    cache=SpeechReceiptCache()
    assert evaluate(cache,[command()]).delivery_verified
    assert not evaluate(cache,[command(status=status,cmd=cmd)]).allows_completion
    assert not evaluate(cache).allows_completion


@pytest.mark.parametrize('status', ['failed','playing','generated'])
def test_conflicting_viewer_evidence_invalidates_cached_success_after_eviction(status):
    cache=SpeechReceiptCache()
    evaluate(cache,[command()])
    assert not evaluate(cache,clips=[command(status=status)]).allows_completion
    assert not evaluate(cache).allows_completion


def test_failed_delivery_stays_failed_even_if_later_snapshot_claims_success():
    cache=SpeechReceiptCache()
    assert evaluate(cache,[command(status='failed')]).state=='failed'
    assert evaluate(cache,[command()]).state=='failed'
    assert evaluate(cache).state=='failed'


def test_duplicate_pending_receipt_is_not_hidden_by_successful_duplicate():
    cache=SpeechReceiptCache()
    evaluate(cache,[command()])
    assert not evaluate(cache,[command(status='executing'),command()]).allows_completion
    assert not evaluate(cache).allows_completion
    # A subsequent unambiguous app completion may establish new evidence.
    assert evaluate(cache,[command()]).delivery_verified


def test_unseen_app_receipt_is_not_fabricated_from_played_viewer_clip():
    cache=SpeechReceiptCache()
    assert not evaluate(cache,clips=[command(status='played')]).allows_completion
    assert not cache.receipts


@pytest.mark.parametrize('scope', [{'map_id':'other'}, {'revision':2}, {'ids':('different',)}])
def test_old_goal_map_or_command_cannot_supply_completion(scope):
    cache=SpeechReceiptCache()
    evaluate(cache,[command()])
    assert not evaluate(cache,**scope).allows_completion
    assert not cache.receipts


def test_cache_copies_only_required_receipt_fields():
    cache=SpeechReceiptCache()
    original={**command(),'text':'Private message'}
    evaluate(cache,[original,command('not-issued')])
    original['status']='failed'
    assert cache.receipts=={'spoken':command()}


def test_bridge_keeps_observed_delivery_when_more_than_100_commands_follow():
    async def check():
        bridge=Bridge(None,None,None,perception='agent',clock=lambda:1.)
        bridge.map_id='home'
        cid=str(uuid4());pose={'x':0.,'y':0.,'yaw':0.,'map_id':'home'}
        state={'map_id':'home','running':True,'intelligence_enabled':True,
               'intelligence_revision':1,'intelligence_goal':'Speak then finish',
               'intelligence_require_speech':True,
               'navigation':{'state':'idle','waypoints':[],'commands':[]},'speech':[]}
        action={'action':'say','text':'Hello','reason':'Speak the requested message'}
        snapshot=[]
        async def request(client,method,path,**kwargs):
            if path in ('/query','/recall'):return {'citations':[]}
            if path=='/events':return []
            if path=='/observation':return {'frame_id':str(uuid4()),'ts':1000,'pose':pose,
                'source':'simulation_render','jpeg_b64':'offline-test'}
            if path=='/state':return state
            if path=='/status':return {'pending_checkin':None}
            if path=='/commands':return snapshot
            if path=='/say':return {'command_id':cid}
            if path=='/plan':
                f=kwargs['json']['observation']
                return {**{k:f[k] for k in ('frame_id','ts','pose')},'action':action,
                    'perception':{'person':False,'posture':'unknown','location':'unknown',
                        'confidence':.9,'caption':'Empty room'},
                    'provider':{'model':'offline-test'},'latency_ms':1}
            raise AssertionError((method,path))
        async def ingest(*args):return {'accepted':True}
        bridge.request,bridge.ingest=request,ingest
        await bridge.think(state)
        assert bridge.pending_agent_command==cid
        snapshot=[command(cid)]
        # Frequent body synchronization sees completion between model turns.
        await bridge.process_commands(state['navigation'],speech_clips=[command(cid,'played')])
        assert bridge.pending_agent_command is None
        snapshot=[command(str(uuid4()),cmd='stop') for _ in range(101)][-100:]
        action={'action':'finish','reason':'Requested speech completed'}
        await bridge.think(state)
        assert bridge.goal_completion['speech_delivery']=='completed'
        # Changing goal scope clears both issued IDs and their retained proof.
        state.update(intelligence_revision=2,intelligence_goal='Speak another message')
        await bridge.think(state)
        assert bridge.goal_completion is None
        assert not bridge.speech_receipts.receipts
    asyncio.run(check())
