import asyncio
import httpx
from robot.simulation.bridge import Bridge


def test_continuous_local_does_not_use_cloud_call_allowance():
    capped=Bridge(None,None,None,max_inferences=1)
    local=Bridge(None,None,None,max_inferences=1,continuous_local=True)
    capped.inferences=local.inferences=25
    assert capped.limit_reached
    assert not local.limit_reached


def test_durable_memory_filters_map_and_keeps_citations():
    async def check():
        cited={'frame_id':'frame-1','ts':100,'caption':'A person is seated',
               'pose':{'x':1.,'y':2.,'yaw':0.,'map_id':'home'}}
        def handler(request):
            return httpx.Response(200,json={'citations':[cited,
                {**cited,'frame_id':'wrong','pose':{**cited['pose'],'map_id':'elsewhere'}}]})
        async with httpx.AsyncClient(base_url='http://test',transport=httpx.MockTransport(handler)) as client:
            bridge=Bridge(None,client,None,clock=lambda:1)
            assert await bridge.retrieve_memories('resident','home')==[cited]
            assert bridge.memory_state['retrieved']==1
    asyncio.run(check())


def test_bridge_lease_excludes_duplicate_delivery(tmp_path):
    import pytest
    from robot.simulation.bridge_lease import body_bridge_lease
    path=tmp_path/'bridge.lock'
    with body_bridge_lease(path):
        with pytest.raises(RuntimeError):
            with body_bridge_lease(path): pass
    with body_bridge_lease(path): pass


def test_scan_keeps_model_planning_available_for_stop_or_speech():
    async def check():
        bridge=Bridge(None,None,None,perception='agent',continuous_local=True)
        state={'ready':True,'map_id':'home','running':True,'intelligence_enabled':True,
               'navigation':{'state':'scanning'}}
        calls=[]
        async def request(*args,**kwargs): return state
        async def body(*args): pass
        async def infer(): calls.append('infer')
        async def think(*args): calls.append('plan')
        bridge.request=request;bridge.body=body;bridge.infer=infer;bridge.think=think
        bridge.pending_agent_command='scan-in-flight'
        await bridge.tick(wait_vision=True)
        assert calls==['plan']
    asyncio.run(check())


def test_paused_autonomy_does_not_cancel_manual_patrol():
    async def check():
        bridge=Bridge(None,None,None,perception='disabled')
        bridge.map_id='home'
        state={'ready':True,'map_id':'home','qpos_base':[0.,0.,.3,1.,0.,0.,0.],
               'running':True,'autonomy_mode':'paused',
               'navigation':{'state':'moving','waypoint':'living-room','waypoints':[],'commands':[]}}
        async def ingest(*args): return {}
        async def commands(*args): pass
        async def coordinate(*args): raise AssertionError('Paused autonomy must not send a stop')
        bridge.ingest=ingest;bridge.process_commands=commands;bridge.coordinate=coordinate
        await bridge.body(state)
    asyncio.run(check())
