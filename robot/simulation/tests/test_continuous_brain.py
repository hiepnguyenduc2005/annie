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
