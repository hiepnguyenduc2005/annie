"""Opt-in Elastic Cloud episodic memory for simulation observations.

Stores a strict, image-free projection of simulation_vlm perceptions in an
existing Elasticsearch index and retrieves them as cited memories. Disabled by
default: from_env returns None unless explicit environment configuration is
present. All failures are explicit and sanitized; the adapter never retries and
never logs credentials.
"""
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from robot.app_backend.app.models import Perception

SEARCH_TIMEOUT_S = 0.5
WRITE_TIMEOUT_S = 0.8
SEARCH_SIZE = 6
INDEX_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{0,99}$')


class MemoryUnavailable(Exception):
    """Configuration or transport failure, sanitized before reaching callers."""


def _sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    return '{0}://{1}'.format(parts.scheme, parts.netloc)


def _validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != 'https':
        raise MemoryUnavailable('elastic url must be https')
    if not parts.hostname or parts.username or parts.password:
        raise MemoryUnavailable('elastic url must not embed credentials')
    if parts.query or parts.fragment:
        raise MemoryUnavailable('elastic url must not contain query or fragment')
    return url.rstrip('/')


class ElasticMemory:
    """Idempotent write/search adapter over one existing Elasticsearch index."""

    def __init__(self, url: str, api_key: str, index: str,
                 client: httpx.AsyncClient | None = None):
        self._url = _validate_url(url)
        self._index = index
        self._headers = {
            'Authorization': 'ApiKey {0}'.format(api_key),
            'Content-Type': 'application/json',
        }
        self._client = client

    async def _send(self, method: str, path: str, json: Any,
                    timeout: float) -> dict:
        url = '{0}/{1}{2}'.format(self._url, self._index, path)
        try:
            if self._client is None:
                async with httpx.AsyncClient(
                        timeout=timeout, headers=self._headers) as client:
                    response = await client.request(method, url, json=json)
            else:
                response = await self._client.request(
                    method, url, json=json, timeout=timeout,
                    headers=self._headers)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise MemoryUnavailable(
                'elastic {0} failed: {1} at {2}'.format(
                    method, type(exc).__name__, _sanitize_url(url))) from exc

    @staticmethod
    def _projection(perception: Perception) -> dict:
        if perception.source != 'simulation_vlm':
            raise MemoryUnavailable(
                'only simulation_vlm perceptions may be persisted')
        pose = perception.pose
        return {
            'frame_id': str(perception.frame_id),
            'ts': perception.ts,
            'map_id': pose.map_id,
            'pose': {'x': pose.x, 'y': pose.y, 'yaw': pose.yaw},
            'caption': perception.caption,
            'person': perception.person,
            'posture': perception.posture,
            'location': perception.location,
            'confidence': perception.confidence,
            'source': perception.source,
        }

    async def upsert(self, perception: Perception) -> str:
        body = self._projection(perception)
        await self._send('PUT', '/_doc/{0}'.format(perception.frame_id),
                         body, WRITE_TIMEOUT_S)
        return str(perception.frame_id)

    @staticmethod
    def _cited_memory(hit: Any) -> dict | None:
        source = hit.get('_source') if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            return None
        frame_id = source.get('frame_id')
        ts = source.get('ts')
        pose = source.get('pose')
        caption = source.get('caption')
        map_id = source.get('map_id')
        if not isinstance(frame_id, str) or not frame_id:
            return None
        if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
            return None
        if not isinstance(caption, str) or not caption:
            return None
        if not isinstance(map_id, str) or not map_id:
            return None
        if not isinstance(pose, dict):
            return None
        x, y, yaw = pose.get('x'), pose.get('y'), pose.get('yaw')
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in (x, y, yaw)):
            return None
        return {
            'caption': caption,
            'frame_id': frame_id,
            'ts': ts,
            'pose': {'x': float(x), 'y': float(y), 'yaw': float(yaw),
                     'map_id': map_id},
        }

    async def search(self, query: str, map_id: str,
                     ts_from: int | None = None,
                     ts_to: int | None = None) -> list[dict]:
        filters: list[dict] = [{'term': {'map_id': map_id}}]
        if ts_from is not None or ts_to is not None:
            ts_range: dict[str, int] = {}
            if ts_from is not None:
                ts_range['gte'] = ts_from
            if ts_to is not None:
                ts_range['lte'] = ts_to
            filters.append({'range': {'ts': ts_range}})
        body = {
            'size': SEARCH_SIZE,
            'query': {'bool': {
                'must': [{'match': {'caption': query}}],
                'filter': filters,
            }},
        }
        payload = await self._send('POST', '/_search', body, SEARCH_TIMEOUT_S)
        hits = payload.get('hits', {}).get('hits', [])
        memories = []
        for hit in hits:
            memory = self._cited_memory(hit)
            if memory is not None and memory['pose']['map_id'] == map_id:
                memories.append(memory)
        return memories


def from_env() -> ElasticMemory | None:
    """Build the adapter from environment, or None when not opted in."""
    if os.environ.get('ANNIE_MEMORY_PROVIDER') != 'elastic':
        return None
    url = os.environ.get('ELASTIC_URL')
    api_key = os.environ.get('ELASTIC_API_KEY')
    index = os.environ.get('ANNIE_MEMORY_INDEX', 'annie-sim-observations')
    if not url or not api_key:
        raise MemoryUnavailable(
            'elastic memory selected but ELASTIC_URL/ELASTIC_API_KEY missing')
    if not INDEX_PATTERN.match(index):
        raise MemoryUnavailable('elastic index name is not a safe pattern')
    return ElasticMemory(url, api_key, index)
