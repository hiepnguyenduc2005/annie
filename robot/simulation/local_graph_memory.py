"""Local Graphiti/FalkorDB memory over already interpreted camera observations.

Graphiti owns graph persistence and hybrid retrieval. Nomic embeddings run on
local Ollama. The vision model has already produced structured observations,
so ingestion never asks a second LLM to invent entities or resident identity.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from uuid import NAMESPACE_URL, uuid5

os.environ['GRAPHITI_TELEMETRY_ENABLED'] = 'false'
os.environ['EMBEDDING_DIM'] = '768'

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from robot.app_backend.app.models import Perception
from robot.simulation.hosted_memory import ElasticMemory


def partition(map_id: str) -> str:
    return 'annie_' + hashlib.sha256(map_id.encode()).hexdigest()[:24]


def stable_id(frame_id: str, suffix: str) -> str:
    return str(uuid5(NAMESPACE_URL, 'annie-observation:' + frame_id + ':' + suffix))


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=500)
    map_id: str = Field(min_length=1, max_length=100)
    ts_from: int | None = Field(default=None, ge=0)
    ts_to: int | None = Field(default=None, ge=0)


class MemoryInputError(ValueError):
    pass


class LocalGraphStore:
    def __init__(self, path: str, *, embedder=None):
        self.path = path
        self.embedder = embedder
        self.drivers = {}
        self.write_lock = asyncio.Lock()

    async def start(self):
        from redislite.falkordb_client import FalkorDB as Embedded
        from falkordb.asyncio import FalkorDB
        from graphiti_core import Graphiti
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.client import EmbedderClient
        from graphiti_core.llm_client.client import LLMClient
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.cross_encoder.client import CrossEncoderClient

        class NoGeneration(LLMClient):
            async def _generate_response(self, *args, **kwargs):
                raise RuntimeError('Graph memory uses structured observations, not LLM extraction')

        class NoCrossEncoder(CrossEncoderClient):
            async def rank(self, *args, **kwargs):
                raise RuntimeError('Graph memory uses reciprocal-rank fusion')

        class OllamaEmbedder(EmbedderClient):
            async def embed(self, text, *, document=False):
                prefix = 'search_document: ' if document else 'search_query: '
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    response = await client.post('http://127.0.0.1:11434/api/embed', json={
                        'model': 'nomic-embed-text:latest', 'input': prefix + text,
                        'truncate': False, 'keep_alive': '30m'})
                    response.raise_for_status()
                    vector = response.json()['embeddings'][0]
                if len(vector) != 768 or not all(type(v) in (int, float) and math.isfinite(v) for v in vector):
                    raise ValueError('Invalid local embedding')
                return vector

            async def create(self, input_data):
                return await self.embed(input_data if isinstance(input_data, str) else input_data[0])

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.embedded = await asyncio.to_thread(Embedded, self.path)
        client = FalkorDB(unix_socket_path=self.embedded.client.socket_file)
        self.driver = FalkorDriver(falkor_db=client, database='annie_metadata')
        await self._ready_driver(self.driver)
        self.embedder = self.embedder or OllamaEmbedder()
        self.graph = Graphiti(graph_driver=self.driver, embedder=self.embedder,
                              llm_client=NoGeneration(LLMConfig(model='disabled')),
                              cross_encoder=NoCrossEncoder(), max_coroutines=2)
        await self.embedder.create('camera observation')  # Warm before serving.

    async def close(self):
        for driver in self.drivers.values():
            await self._ready_driver(driver)
        await self.driver.close()
        await asyncio.to_thread(self.embedded.client.save)
        await asyncio.to_thread(self.embedded.client.shutdown)

    @staticmethod
    async def _ready_driver(driver):
        # The pinned FalkorDriver schedules index creation in its constructor.
        # Join that work instead of launching duplicate background index tasks.
        task = getattr(driver, '_init_task', None)
        if task is not None:
            await task
        else:
            await driver.build_indices_and_constraints()

    async def _driver(self, map_id):
        group = partition(map_id)
        if group not in self.drivers:
            names = await asyncio.to_thread(self.embedded.list_graphs)
            if group not in names and len([n for n in names if n.startswith('annie_')]) >= 17:
                raise MemoryInputError('Local memory map capacity reached')
            driver = self.driver.clone(database=group)
            await self._ready_driver(driver)
            self.drivers[group] = driver
        return self.drivers[group]

    async def upsert(self, observation: Perception):
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        from graphiti_core.edges import EntityEdge, EpisodicEdge

        if observation.ts > int(time.time() * 1000):
            raise MemoryInputError('Future observations are not evidence')
        body = ElasticMemory._projection(observation)
        content = json.dumps(body, sort_keys=True, separators=(',', ':'))
        frame_id, map_id = body['frame_id'], body['map_id']
        async with self.write_lock:
            driver = await self._driver(map_id)
            old, _, _ = await driver.execute_query(
                'MATCH (e:Episodic {uuid:$uuid}) RETURN e.content AS content', uuid=frame_id)
            if old:
                if old[0]['content'] != content:
                    raise MemoryInputError('Capture ID already contains different evidence')
                return {'frame_id': frame_id, 'status': 'already_indexed'}
            count, _, _ = await driver.execute_query('MATCH (e:Episodic) RETURN count(e) AS count')
            if count[0]['count'] >= 1000:
                raise MemoryInputError('Local memory observation capacity reached')
            group = partition(map_id)
            capture_time = datetime.fromtimestamp(body['ts'] / 1000, timezone.utc)
            observer = EntityNode(uuid=stable_id(frame_id, 'observer'), name='Camera observation ' + frame_id,
                                  group_id=group, labels=['Observation'])
            place = EntityNode(uuid=stable_id(map_id, 'map'), name='Map ' + map_id,
                               group_id=group, labels=['Map'])
            edge = EntityEdge(uuid=stable_id(frame_id, 'observed_in'), source_node_uuid=observer.uuid,
                              target_node_uuid=place.uuid, name='OBSERVED_IN', fact=body['caption'],
                              episodes=[frame_id], group_id=group, valid_at=capture_time,
                              created_at=datetime.now(timezone.utc))
            edge.fact_embedding = await self.embedder.embed(body['caption'], document=True)
            await observer.save(driver)
            await place.save(driver)
            await edge.save(driver)
            episode = EpisodicNode(uuid=frame_id, name='Camera frame ' + frame_id, group_id=group,
                source=EpisodeType.json, source_description='simulation_vlm; observer pose, not person position',
                content=content, valid_at=capture_time, entity_edges=[edge.uuid])
            await episode.save(driver)
            await EpisodicEdge(uuid=stable_id(frame_id, 'mentions'), source_node_uuid=frame_id,
                               target_node_uuid=observer.uuid, group_id=group,
                               created_at=datetime.now(timezone.utc)).save(driver)
            await asyncio.to_thread(self.embedded.client.save)
            return {'frame_id': frame_id, 'status': 'indexed'}

    async def search(self, request: SearchRequest):
        from graphiti_core.nodes import EpisodicNode
        from graphiti_core.search.search import search
        from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
        from graphiti_core.search.search_filters import SearchFilters, DateFilter, ComparisonOperator

        if partition(request.map_id) not in await asyncio.to_thread(self.embedded.list_graphs):
            return []
        upper = min(request.ts_to if request.ts_to is not None else int(time.time() * 1000),
                    int(time.time() * 1000))
        lower = request.ts_from or 0
        if lower > upper:
            return []
        filters = SearchFilters(valid_at=[[
            DateFilter(date=datetime.fromtimestamp(lower / 1000, timezone.utc), comparison_operator=ComparisonOperator.greater_than_equal),
            DateFilter(date=datetime.fromtimestamp(upper / 1000, timezone.utc), comparison_operator=ComparisonOperator.less_than_equal)]])
        config = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
        config.limit = 12
        driver = await self._driver(request.map_id)
        # Bind our initialized partition driver. The high-level decorator would
        # allocate another driver and start index creation on every query.
        result = await search(self.graph.clients, request.query, [partition(request.map_id)],
                              config, filters, driver=driver)
        ids = list(dict.fromkeys(uid for edge in result.edges for uid in edge.episodes))[:12]
        if not ids:
            return []
        episodes = {e.uuid: e for e in await EpisodicNode.get_by_uuids(driver, ids)}
        citations = []
        for uid in ids:
            episode = episodes.get(uid)
            if not episode:
                continue
            body = json.loads(episode.content)
            citation = ElasticMemory._cited_memory({'_source': body})
            if citation and citation['pose']['map_id'] == request.map_id and lower <= citation['ts'] <= upper:
                citations.append(citation)
            if len(citations) == 6:
                break
        return citations

    async def health(self):
        counts = 0
        for name in await asyncio.to_thread(self.embedded.list_graphs):
            if name.startswith('annie_') and name != 'annie_metadata':
                result = await self.driver.client.select_graph(name).ro_query(
                    'MATCH (e:Episodic) RETURN count(e)')
                counts += result.result_set[0][0]
        return {'status': 'ready', 'provider': 'graphiti_local', 'database': 'falkordblite',
                'embedding_model': 'nomic-embed-text:latest', 'observations': counts,
                'retrieval': 'graphiti_hybrid_rrf', 'cloud_calls': 0}


def create_app(store):
    @asynccontextmanager
    async def lifespan(app):
        await store.start()
        yield
        await store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def local_requests(request: Request, call_next):
        from starlette.responses import JSONResponse
        if request.headers.get('origin'):
            return JSONResponse({'detail': 'Browser-origin requests are not accepted'}, status_code=403)
        if request.method == 'POST':
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 32768:
                    return JSONResponse({'detail': 'Request exceeds limit'}, status_code=413)
            request._body = bytes(data)
        return await call_next(request)

    @app.get('/health')
    async def health():
        return await store.health()

    @app.post('/observations')
    async def upsert(observation: Perception):
        if observation.source != 'simulation_vlm':
            raise HTTPException(422, 'Expected simulation_vlm evidence')
        try:
            return await store.upsert(observation)
        except MemoryInputError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(503, 'Local graph indexing unavailable') from None

    @app.post('/search')
    async def search(request: SearchRequest):
        try:
            return {'provider': 'graphiti_local', 'citations': await store.search(request)}
        except Exception:
            raise HTTPException(503, 'Local graph retrieval unavailable') from None
    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8005)
    parser.add_argument('--path', default='.data/simulation/graphiti/observations.rdb')
    args = parser.parse_args()
    uvicorn.run(create_app(LocalGraphStore(args.path)), host='127.0.0.1', port=args.port,
                access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
