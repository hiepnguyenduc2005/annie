"""Bounded HTTP adapter for the isolated, loopback-only Graphiti service."""
from urllib.parse import urlsplit
import asyncio
import ipaddress
import json
import time

import httpx

from robot.simulation.hosted_memory import ElasticMemory, MemoryUnavailable


class GraphitiMemory:
    provider_name = 'graphiti_local'

    def __init__(self, url='http://127.0.0.1:8005', *, client=None):
        parts = urlsplit(url)
        try:
            local = parts.hostname == 'localhost' or ipaddress.ip_address(parts.hostname).is_loopback
            _ = parts.port
        except (ValueError, TypeError):
            local = False
        if not local or parts.scheme != 'http' or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
            raise MemoryUnavailable('Graphiti service must use an HTTP loopback URL without credentials')
        self.url = url.rstrip('/').replace('://localhost', '://127.0.0.1', 1)
        self.client = client

    async def _post(self, path, body, timeout):
        async def exchange(client):
            async with asyncio.timeout(timeout):
                async with client.stream('POST', self.url + path, json=body, timeout=timeout) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 32768:
                            raise MemoryUnavailable('Local graph response exceeds limit')
                    return json.loads(data)
        try:
            if self.client:
                return await exchange(self.client)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await exchange(client)
        except Exception as exc:
            raise MemoryUnavailable('Local graph request failed: ' + type(exc).__name__) from None

    async def upsert(self, observation):
        ElasticMemory._projection(observation)  # Enforce the source boundary before I/O.
        result = await self._post('/observations', observation.model_dump(mode='json'), 1.2)
        if result.get('frame_id') != str(observation.frame_id) or result.get('status') not in ('indexed', 'already_indexed'):
            raise MemoryUnavailable('Local graph did not acknowledge this observation')
        return str(observation.frame_id)

    async def search(self, query, map_id, ts_from=None, ts_to=None):
        upper = min(int(time.time() * 1000), ts_to if ts_to is not None else int(time.time() * 1000))
        lower = ts_from or 0
        result = await self._post('/search', {'query': query, 'map_id': map_id,
            'ts_from': lower, 'ts_to': upper}, 1.0)
        citations = []
        for item in result.get('citations', [])[:6]:
            if not isinstance(item, dict) or not isinstance(item.get('pose'), dict):
                continue
            citation = ElasticMemory._cited_memory({'_source': {**item, 'map_id': item['pose'].get('map_id')}})
            if citation and citation['pose']['map_id'] == map_id and lower <= citation['ts'] <= upper:
                citations.append(citation)
        return citations
