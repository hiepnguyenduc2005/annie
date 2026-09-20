"""Goal-conditioned recall over stored perception captions.

Captions written by Service.observe are embedded once and ranked against a
planner goal by cosine similarity, restricted to one map and an upper capture
time. Every hit cites a stored frame with its original capture identity
(frame_id, ts, pose); nothing is re-dated, summarized, or synthesized.

The embedding provider is optional and untrusted: it may be slow, down, or
return malformed vectors. None of that raises out of index() or recall().
A failed provider is left alone for a cooldown so one hung call cannot stall
every perception frame, and recall degrades to deterministic lexical overlap.

Only caption text is sent to the embedder; no images, poses, or identifiers.
"""
import json
import re
import time
from math import isfinite, sqrt

from .episodic_memory import STOP, _single


def ollama_embedder(url, model, timeout_s=3.0, *, transport=None):
    """Return callable(list[str]) -> list[list[float]] backed by Ollama /api/embed.

    Raises on transport, status, or shape errors; SemanticMemory absorbs them.
    trust_env=False keeps resident captions off any ambient HTTP proxy.
    """
    import httpx
    endpoint = url.rstrip('/') + '/api/embed'

    def embed(texts):
        with httpx.Client(trust_env=False, timeout=timeout_s, transport=transport) as client:
            response = client.post(endpoint, json={'model': model, 'input': list(texts)})
            response.raise_for_status()
            return response.json()['embeddings']
    return embed


def disabled_embedder(texts):
    raise RuntimeError('semantic embeddings are not configured')


def _tokens(text):
    return {_single(token) for token in re.findall(r'\w+', text.lower())}


def _cosine(a, b, norm_a):
    norm_b = sqrt(sum(value * value for value in b))
    if not norm_b:
        return None
    return sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)


class SemanticMemory:
    def __init__(self, db, *, embedder, dims_limit=2048, cooldown_s=30.0, clock=time.monotonic):
        self.db, self.embedder, self.dims_limit = db, embedder, dims_limit
        self.cooldown_s, self.clock = cooldown_s, clock
        self._retry_at = None
        db.execute('CREATE TABLE IF NOT EXISTS memory_embeddings '
                   '(id TEXT PRIMARY KEY, map_id TEXT, ts INTEGER, vec BLOB)')
        db.commit()

    def _embed(self, text):
        """One validated vector, or None when the provider is unusable."""
        if not text.strip():
            return None  # Nothing to embed; not a provider failure.
        now = self.clock()
        if self._retry_at is not None and now < self._retry_at:
            return None
        try:
            vectors = self.embedder([text])
            vector = [float(value) for value in vectors[0]] if len(vectors) == 1 else None
            if (not vector or len(vector) > self.dims_limit
                    or not all(isfinite(value) for value in vector) or not any(vector)):
                vector = None
        except Exception:
            vector = None
        self._retry_at = None if vector else now + self.cooldown_s
        return vector

    def index(self, frame_id, map_id, ts, caption):
        """Embed one stored frame's caption. Never commits: the caller owns the
        transaction (Service.observe's atomic boundary)."""
        vector = self._embed(caption)
        if vector is None:
            return False
        self.db.execute('INSERT OR REPLACE INTO memory_embeddings VALUES (?, ?, ?, ?)',
                        (str(frame_id), map_id, int(ts), json.dumps(vector)))
        # The memory table is bounded; embeddings of pruned frames go with it.
        self.db.execute('DELETE FROM memory_embeddings WHERE id NOT IN (SELECT id FROM memory)')
        return True

    def recall(self, goal, *, map_id, ts_to, limit=6):
        indexed = self.db.execute(
            'SELECT COUNT(*) FROM memory_embeddings e JOIN memory m ON m.id = e.id '
            'WHERE e.map_id = ? AND e.ts <= ?', (map_id, ts_to)).fetchone()[0]
        vector = self._embed(goal)
        if vector is None:
            scored, provider = self._lexical(goal, map_id, ts_to), 'lexical_fallback'
        else:
            scored, provider = self._semantic(vector, map_id, ts_to), 'embeddings'
        scored.sort(key=lambda pair: (pair[0], pair[1]['ts']), reverse=True)
        citations = [{'caption': item['caption'], 'frame_id': item['frame_id'], 'ts': item['ts'],
                      'pose': item['pose'], 'score': round(score, 6)}
                     for score, item in scored[:max(0, limit)]]
        return {'citations': citations, 'provider': provider, 'indexed': indexed}

    def _semantic(self, goal_vector, map_id, ts_to):
        goal_norm = sqrt(sum(value * value for value in goal_vector))
        scored = []
        rows = self.db.execute(
            'SELECT e.vec, m.payload FROM memory_embeddings e JOIN memory m ON m.id = e.id '
            'WHERE e.map_id = ? AND e.ts <= ?', (map_id, ts_to))
        for vec, payload in rows:
            stored = json.loads(vec)
            # A different length means a different embedding model; not comparable.
            if len(stored) != len(goal_vector):
                continue
            score = _cosine(goal_vector, stored, goal_norm)
            if score is not None:
                scored.append((score, json.loads(payload)))
        return scored

    def _lexical(self, goal, map_id, ts_to):
        every = _tokens(goal)
        wanted = {token for token in every if token not in {_single(word) for word in STOP}} or every
        scored = []
        if not wanted:
            return scored
        for (payload,) in self.db.execute('SELECT payload FROM memory WHERE ts <= ?', (ts_to,)):
            item = json.loads(payload)
            if item['pose'].get('map_id') != map_id:
                continue
            shared = len(wanted & _tokens(item['caption']))
            if shared:  # No shared content token is no evidence of relevance.
                scored.append((shared / len(wanted), item))
        return scored
