import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from robot.app_backend.app.models import Perception
from robot.simulation.graph_memory_client import GraphitiMemory
from robot.simulation.hosted_memory import MemoryUnavailable, from_env
from robot.simulation.local_graph_memory import LocalGraphStore, SearchRequest, create_app


def observation(**changes):
    fields = dict(frame_id=str(uuid4()), ts=int(time.time()*1000)-1000,
        person=True, posture='standing', location='unknown', confidence=0.9,
        caption='A person beside the bed.', source='simulation_vlm',
        pose={'map_id':'test-home','x':1.2,'y':2.3,'yaw':0.4})
    fields.update(changes)
    return Perception.model_validate(fields)


@pytest.mark.parametrize('url', ['https://127.0.0.1:8005', 'http://external.example',
    'http://127.0.0.1:8005/path', 'http://secret@127.0.0.1:8005', 'http://127.0.0.1:8005?x=1'])
def test_client_rejects_nonlocal_or_ambiguous_endpoint(url):
    with pytest.raises(MemoryUnavailable):
        GraphitiMemory(url)


def test_env_selects_local_graph(monkeypatch):
    monkeypatch.setenv('ANNIE_MEMORY_PROVIDER','graphiti')
    monkeypatch.delenv('ANNIE_GRAPHITI_URL', raising=False)
    assert from_env().provider_name == 'graphiti_local'


def test_client_rejects_wrong_map_future_and_malformed_citations():
    now = int(time.time()*1000)
    cited = dict(frame_id='frame-one',caption='Person visible',ts=now-10,
                 pose={'x':1,'y':2,'yaw':0,'map_id':'home'})
    async def run():
        def handle(request):
            assert json.loads(request.content)['ts_to'] <= now
            return httpx.Response(200,json={'citations':[cited,{**cited,'ts':now+1},
                {**cited,'pose':{**cited['pose'],'map_id':'another-map'}}, {'caption':'bad'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result=await GraphitiMemory(client=client).search('person','home',ts_to=now)
            assert result == [cited]
    asyncio.run(run())


def test_client_requires_indexed_receipt_and_rejects_hardware_without_io():
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json={'frame_id':'wrong','status':'queued'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter=GraphitiMemory(client=client)
            with pytest.raises(MemoryUnavailable):
                await adapter.upsert(observation(source='mock'))
            assert not calls
            with pytest.raises(MemoryUnavailable):
                await adapter.upsert(observation())
            assert len(calls)==1
    asyncio.run(run())


def test_http_api_bounds_input_and_sanitizes_internal_errors():
    class BrokenStore:
        async def start(self): pass
        async def close(self): pass
        async def health(self): return {'status':'ready'}
        async def upsert(self, value): raise RuntimeError('secret provider response')
        async def search(self, value): raise RuntimeError('secret provider response')
    with TestClient(create_app(BrokenStore())) as client:
        assert client.get('/health',headers={'Origin':'https://evil.example'}).status_code==403
        assert client.post('/observations',content=b'x'*32769).status_code==413
        response=client.post('/observations',json=observation().model_dump(mode='json'))
        assert response.status_code==503
        assert 'secret' not in response.text
        assert client.post('/observations',json=observation(source='mock').model_dump(mode='json')).status_code==422


def test_actual_graph_persistence_idempotency_map_and_time_filters(tmp_path):
    pytest.importorskip('graphiti_core')
    from graphiti_core.embedder.client import EmbedderClient
    class Embeddings(EmbedderClient):
        async def create(self, input_data): return [1.0]+[0.0]*767
        async def embed(self, text, *, document=False): return await self.create(text)
    async def run():
        path=str(tmp_path/'observations.rdb')
        store=LocalGraphStore(path,embedder=Embeddings())
        first=observation()
        await store.start()
        try:
            assert (await store.upsert(first))['status']=='indexed'
            assert (await store.upsert(first))['status']=='already_indexed'
            with pytest.raises(ValueError,match='different evidence'):
                await store.upsert(first.model_copy(update={'caption':'Contradictory replacement'}))
            assert (await store.health())['observations']==1
            hits=await store.search(SearchRequest(query='person beside bed',map_id='test-home'))
            assert hits[0]['frame_id']==str(first.frame_id)
            assert hits[0]['ts']==first.ts
            assert hits[0]['pose']==first.pose.model_dump()
            assert await store.search(SearchRequest(query='person',map_id='other-home'))==[]
            assert await store.search(SearchRequest(query='person',map_id='test-home',ts_to=first.ts-1))==[]
        finally:
            await store.close()
        reopened=LocalGraphStore(path,embedder=Embeddings())
        await reopened.start()
        try:
            hits=await reopened.search(SearchRequest(query='person beside bed',map_id='test-home'))
            assert hits[0]['frame_id']==str(first.frame_id)
        finally:
            await reopened.close()
    asyncio.run(run())
