import json, sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from robot.app_backend.app.main import create_app
from robot.app_backend.app.semantic_memory import SemanticMemory, ollama_embedder
from robot.app_backend.app.service import Service

VEC = {'person on the floor in the living room': [1.0, 0.0], 'empty kitchen counter': [0.0, 1.0],
       'where is the person': [0.9, 0.1], 'kitchen': [0.1, 0.9]}

def fake_embedder(texts):
    return [VEC[t] for t in texts]

def seed(db):
    db.execute('CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
    for fid, ts, cap in [('f1', 100, 'person on the floor in the living room'), ('f2', 200, 'empty kitchen counter')]:
        db.execute('INSERT INTO memory VALUES (?,?,?)', (fid, ts, json.dumps({'caption': cap, 'frame_id': fid, 'ts': ts,
                   'pose': {'x': 0, 'y': 0, 'yaw': 0, 'map_id': 'house'}})))

def test_recall_ranks_by_goal_similarity_with_citations():
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=fake_embedder)
    assert m.index('f1', 'house', 100, 'person on the floor in the living room')
    assert m.index('f2', 'house', 200, 'empty kitchen counter')
    r = m.recall('where is the person', map_id='house', ts_to=1000)
    assert r['provider'] == 'embeddings' and [c['frame_id'] for c in r['citations']] == ['f1', 'f2']
    assert r['citations'][0]['pose']['map_id'] == 'house' and r['citations'][0]['score'] > r['citations'][1]['score']

def test_recall_filters_map_and_time():
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=fake_embedder)
    m.index('f1', 'house', 100, 'person on the floor in the living room'); m.index('f2', 'garage', 200, 'empty kitchen counter')
    assert [c['frame_id'] for c in m.recall('kitchen', map_id='house', ts_to=1000)['citations']] == ['f1']
    assert m.recall('kitchen', map_id='house', ts_to=50)['citations'] == []

def test_embedder_failure_falls_back_to_lexical_and_never_raises():
    db = sqlite3.connect(':memory:'); seed(db)
    calls = {'n': 0}
    def flaky(texts):
        calls['n'] += 1
        raise RuntimeError('ollama down')
    m = SemanticMemory(db, embedder=flaky)
    assert m.index('f1', 'house', 100, 'person on the floor in the living room') is False
    r = m.recall('person floor', map_id='house', ts_to=1000)
    assert r['provider'] == 'lexical_fallback' and r['citations'][0]['frame_id'] == 'f1'


def test_recall_reports_shape_limit_and_indexed_count():
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=fake_embedder)
    m.index('f1', 'house', 100, 'person on the floor in the living room')
    m.index('f2', 'house', 200, 'empty kitchen counter')
    r = m.recall('kitchen', map_id='house', ts_to=1000, limit=1)
    assert r['indexed'] == 2 and [c['frame_id'] for c in r['citations']] == ['f2']
    assert set(r['citations'][0]) == {'caption', 'frame_id', 'ts', 'pose', 'score'}
    assert r['citations'][0]['caption'] == 'empty kitchen counter' and r['citations'][0]['ts'] == 200
    # Re-indexing a retried frame is idempotent, never a duplicate citation.
    assert m.index('f2', 'house', 200, 'empty kitchen counter')
    assert m.recall('kitchen', map_id='house', ts_to=1000)['indexed'] == 2


def test_lexical_fallback_filters_map_and_time_and_omits_unrelated_frames():
    db = sqlite3.connect(':memory:'); seed(db)
    def down(texts):
        raise RuntimeError('ollama down')
    m = SemanticMemory(db, embedder=down)
    r = m.recall('Where is the PERSON?', map_id='house', ts_to=1000)
    # Stop words ("where", "is", "the") never make an unrelated caption a hit.
    assert [c['frame_id'] for c in r['citations']] == ['f1'] and r['indexed'] == 0
    assert m.recall('person', map_id='garage', ts_to=1000)['citations'] == []
    assert m.recall('person', map_id='house', ts_to=50)['citations'] == []


@pytest.mark.parametrize('bad', [
    [[float('nan'), 1.0]], [[1.0] * 5], [[]], [['a', 'b']], [[0.0, 0.0]], [], 'nope', None,
])
def test_malformed_embedder_output_is_a_failure_not_an_index_entry(bad):
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=lambda texts: bad, dims_limit=4)
    assert m.index('f1', 'house', 100, 'person on the floor in the living room') is False
    r = m.recall('person', map_id='house', ts_to=1000)
    assert r['provider'] == 'lexical_fallback' and r['indexed'] == 0


def test_vectors_from_a_different_model_dimension_are_skipped():
    db = sqlite3.connect(':memory:'); seed(db)
    vectors = {'person on the floor in the living room': [1.0, 0.0, 0.0], 'empty kitchen counter': [0.0, 1.0],
               'kitchen': [0.1, 0.9]}
    m = SemanticMemory(db, embedder=lambda texts: [vectors[t] for t in texts])
    m.index('f1', 'house', 100, 'person on the floor in the living room')
    m.index('f2', 'house', 200, 'empty kitchen counter')
    assert [c['frame_id'] for c in m.recall('kitchen', map_id='house', ts_to=1000)['citations']] == ['f2']


def test_failed_embedder_is_not_retried_until_the_cooldown_passes():
    db = sqlite3.connect(':memory:'); seed(db)
    clock, calls, healthy = [0.0], [], [False]
    def embedder(texts):
        calls.append(texts)
        if not healthy[0]:
            raise TimeoutError('model still loading')
        return [VEC[t] for t in texts]
    m = SemanticMemory(db, embedder=embedder, cooldown_s=30, clock=lambda: clock[0])
    assert m.index('f1', 'house', 100, 'person on the floor in the living room') is False
    healthy[0] = True
    clock[0] = 29.0
    # A slow or hung provider must not block every perception frame.
    assert m.index('f1', 'house', 100, 'person on the floor in the living room') is False
    assert m.recall('kitchen', map_id='house', ts_to=1000)['provider'] == 'lexical_fallback'
    assert len(calls) == 1
    clock[0] = 30.0
    assert m.index('f2', 'house', 200, 'empty kitchen counter') is True
    assert m.recall('kitchen', map_id='house', ts_to=1000)['provider'] == 'embeddings'


def test_blank_text_is_skipped_without_calling_or_penalizing_the_embedder():
    db = sqlite3.connect(':memory:'); seed(db)
    calls = []
    def embedder(texts):
        calls.append(texts)
        return [VEC[t] for t in texts]
    m = SemanticMemory(db, embedder=embedder)
    assert m.index('f1', 'house', 100, '  ') is False
    assert m.recall(' ', map_id='house', ts_to=1000) == {'citations': [], 'provider': 'lexical_fallback', 'indexed': 0}
    assert calls == []
    assert m.index('f2', 'house', 200, 'empty kitchen counter') is True


def test_embeddings_of_pruned_frames_are_neither_cited_nor_kept():
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=fake_embedder)
    m.index('f1', 'house', 100, 'person on the floor in the living room')
    db.execute("DELETE FROM memory WHERE id='f1'")
    assert m.recall('kitchen', map_id='house', ts_to=1000)['citations'] == []
    m.index('f2', 'house', 200, 'empty kitchen counter')
    assert [row[0] for row in db.execute('SELECT id FROM memory_embeddings')] == ['f2']


def test_ollama_embedder_posts_the_embed_request_without_proxy_env():
    seen = {}
    def handler(request):
        seen['url'], seen['body'] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={'embeddings': [[0.5, 0.5], [1.0, 0.0]]})
    embed = ollama_embedder('http://127.0.0.1:11434/', 'nomic-embed-text', transport=httpx.MockTransport(handler))
    assert embed(['a', 'b']) == [[0.5, 0.5], [1.0, 0.0]]
    assert seen == {'url': 'http://127.0.0.1:11434/api/embed',
                    'body': {'model': 'nomic-embed-text', 'input': ['a', 'b']}}


def test_ollama_embedder_raises_on_provider_error_for_the_caller_to_absorb():
    failing = ollama_embedder('http://127.0.0.1:11434', 'nomic-embed-text',
                              transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(httpx.HTTPError):
        failing(['a'])
    db = sqlite3.connect(':memory:'); seed(db)
    m = SemanticMemory(db, embedder=failing)
    assert m.index('f1', 'house', 100, 'person on the floor in the living room') is False
    assert m.recall('person', map_id='house', ts_to=1000)['provider'] == 'lexical_fallback'


def frame(ts, caption, map_id='house'):
    return {'ts': ts, 'frame_id': str(uuid4()), 'person': False, 'posture': 'unknown', 'location': 'unknown',
            'confidence': .9, 'caption': caption, 'pose': {'x': 1, 'y': 2, 'map_id': map_id}}


def test_observe_indexes_accepted_frames_once():
    now, embedded = [10_000], []
    def embedder(texts):
        embedded.extend(texts)
        return [VEC.get(t, [0.5, 0.5]) for t in texts]
    service = Service(':memory:', clock=lambda: now[0], embedder=embedder)
    first = frame(now[0], 'empty kitchen counter')
    assert service.ingest('brain.perception', first)
    assert not service.ingest('brain.perception', first)  # Replay: not re-embedded.
    stale = frame(now[0] - 6000, 'person on the floor in the living room')
    assert not service.ingest('brain.perception', stale)  # Rejected frames are never indexed.
    assert embedded == ['empty kitchen counter']
    r = service.recall('kitchen', 'house')
    assert r['provider'] == 'embeddings' and r['indexed'] == 1
    assert r['citations'][0]['frame_id'] == first['frame_id'] and r['citations'][0]['ts'] == first['ts']


def test_observe_survives_a_failing_embedder_and_recall_never_cites_the_future():
    now = [10_000]
    def down(texts):
        raise ConnectionError('ollama down')
    service = Service(':memory:', clock=lambda: now[0], embedder=down)
    assert service.ingest('brain.perception', frame(now[0], 'empty kitchen counter'))
    assert service.perception['caption'] == 'empty kitchen counter'
    r = service.recall('kitchen', 'house')
    assert r['provider'] == 'lexical_fallback' and len(r['citations']) == 1
    # ts_to bounds evidence; a caller cannot widen it past the service clock.
    assert service.recall('kitchen', 'house', ts_to=now[0] - 1)['citations'] == []
    now[0] -= 1
    assert service.recall('kitchen', 'house', ts_to=10**15)['citations'] == []


@pytest.fixture
def client(monkeypatch):
    # Offline by construction: without a configured model the embedder raises
    # and recall answers from the lexical fallback.
    monkeypatch.delenv('ANNIE_EMBED_MODEL', raising=False)
    with TestClient(create_app(':memory:', token='')) as client:
        yield client


def test_recall_route_returns_citations_and_rejects_bad_input(client):
    assert client.post('/demo/seed').status_code == 200
    response = client.post('/recall', json={'goal': 'where are the glasses', 'map_id': 'demo-home'})
    assert response.status_code == 200
    body = response.json()
    assert body['provider'] == 'lexical_fallback' and body['indexed'] == 0
    assert set(body['citations'][0]) == {'caption', 'frame_id', 'ts', 'pose', 'score'}
    assert 'glasses' in body['citations'][0]['caption']
    assert body['citations'][0]['pose']['map_id'] == 'demo-home'
    other = client.post('/recall', json={'goal': 'glasses', 'map_id': 'other-map', 'ts_to': None, 'limit': 1})
    assert other.status_code == 200 and other.json()['citations'] == []
    ok = {'goal': 'glasses', 'map_id': 'demo-home'}
    for bad in ({**ok, 'goal': ''}, {**ok, 'goal': 'x' * 501}, {'goal': 'glasses'}, {**ok, 'map_id': ''},
                {**ok, 'limit': 0}, {**ok, 'limit': 7}, {**ok, 'ts_to': '5'}, {**ok, 'ts_to': -1},
                {**ok, 'unexpected': True}):
        assert client.post('/recall', json=bad).status_code == 422, bad


def test_recall_route_requires_the_same_auth_as_query(monkeypatch):
    monkeypatch.delenv('ANNIE_EMBED_MODEL', raising=False)
    body = {'goal': 'glasses', 'map_id': 'demo-home'}
    with TestClient(create_app(':memory:', token='secret')) as client:
        assert client.post('/recall', json=body).status_code == 401
        assert client.post('/recall', json=body, headers={'Authorization': 'Bearer secret'}).status_code == 200
    with TestClient(create_app(':memory:', token=''), client=('198.51.100.1', 42)) as client:
        assert client.post('/recall', json=body).status_code == 403
