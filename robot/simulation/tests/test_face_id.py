# robot/simulation/tests/test_face_id.py
import json
import sys

import pytest

from robot.simulation.face_id import DEFAULT_THRESHOLD, FaceIdError, FaceIndex, enroll_directory
from robot.simulation.person_tracker import PersonTracker

# Fake embedder: the "JPEG" bytes name a canned 3-d vector, so no model loads.
VECTORS = {
    b'ellis-1': [1.0, 0.0, 0.0],
    b'ellis-2': [0.9, 0.1, 0.0],
    b'ellis-live': [0.95, 0.05, 0.0],
    b'morgan-1': [0.0, 1.0, 0.0],
    b'stranger': [0.0, 0.0, 1.0],
    b'borderline': [0.4, 0.0, 0.9165],  # cosine 0.4 to ellis-1: just under 0.45
}

def fake_embedder(jpeg, box):
    return VECTORS.get(jpeg)  # None = no usable face in that region

def index(**kw):
    return FaceIndex(embedder=fake_embedder, **kw)

def upright_det(track_id, box=(90, 30, 150, 260)):
    pts = [(0.0, 0.0)] * 17; c = [0.0] * 17
    pts[5], pts[6], pts[11], pts[12] = (100, 50), (140, 50), (105, 150), (135, 150)
    for i in (5, 6, 11, 12): c[i] = 0.9
    return {'track_id': track_id, 'box': list(box), 'conf': 0.8, 'keypoints': pts, 'kp_conf': c}

def test_module_imports_without_the_face_backend():
    assert 'insightface' not in sys.modules  # lazy: only the real-model path loads it

def test_enroll_counts_only_images_with_a_face_and_is_idempotent():
    idx = index()
    assert idx.enroll('Ellis', [b'ellis-1', b'ellis-2', b'no-face-here']) == 2
    assert idx.enroll('Ellis', [b'ellis-1']) == 0  # same photo again adds nothing
    assert idx.enroll('Morgan', [b'morgan-1']) == 1
    assert idx.names() == ['Ellis', 'Morgan']

def test_identify_returns_best_name_and_cosine_score():
    idx = index()
    idx.enroll('Ellis', [b'ellis-1', b'ellis-2']); idx.enroll('Morgan', [b'morgan-1'])
    hit = idx.identify(b'ellis-live', [10, 10, 50, 90])
    assert hit['name'] == 'Ellis' and 0.99 < hit['score'] <= 1.0
    assert idx.identify(b'morgan-1', [10, 10, 50, 90]) == {'name': 'Morgan', 'score': 1.0}

def test_unknown_face_no_face_and_empty_index_return_none():
    idx = index()
    assert idx.identify(b'ellis-live', [0, 0, 5, 5]) is None  # nobody enrolled
    idx.enroll('Ellis', [b'ellis-1'])
    assert idx.identify(b'stranger', [0, 0, 5, 5]) is None
    assert idx.identify(b'no-face-here', [0, 0, 5, 5]) is None

def test_threshold_is_the_boundary_between_a_match_and_none():
    assert DEFAULT_THRESHOLD == 0.45
    strict, loose = index(), index(threshold=0.35)
    for idx in (strict, loose): idx.enroll('Ellis', [b'ellis-1'])
    assert strict.identify(b'borderline', None) is None
    assert loose.identify(b'borderline', None) == {'name': 'Ellis', 'score': 0.4}

def test_save_load_round_trip_keeps_names_and_matches(tmp_path):
    idx = index()
    idx.enroll('Ellis', [b'ellis-1', b'ellis-2']); idx.enroll('Morgan', [b'morgan-1'])
    path = tmp_path / 'nested' / 'index.json'
    idx.save(path)
    stored = json.loads(path.read_text())
    assert len(stored['Ellis']) == 2 and len(stored['Ellis'][0]) == 3  # name -> embedding lists
    again = index().load(path)
    assert again.names() == ['Ellis', 'Morgan']
    assert again.identify(b'ellis-live', None) == idx.identify(b'ellis-live', None)

def test_load_rejects_missing_corrupt_and_other_backend_files(tmp_path):
    with pytest.raises(FaceIdError, match='not found'):
        index().load(tmp_path / 'absent.json')
    bad = tmp_path / 'bad.json'; bad.write_text('{"Ellis": "not-a-list"}')
    with pytest.raises(FaceIdError, match='invalid'):
        index().load(bad)
    other = tmp_path / 'other.json'
    other.write_text(json.dumps({'_meta': {'backend': 'insightface/buffalo_l'}, 'Ellis': [[1.0, 0.0, 0.0]]}))
    with pytest.raises(FaceIdError, match='buffalo_l'):
        index().load(other)  # embeddings from another model are not comparable

def test_forget_removes_a_person_and_bad_names_are_rejected():
    idx = index()
    idx.enroll('Ellis', [b'ellis-1'])
    assert idx.forget('Ellis') == 1 and idx.names() == [] and idx.forget('Ellis') == 0
    for bad in ('', '  ', '../etc', '_meta', 'a/b'):
        with pytest.raises(ValueError):
            idx.enroll(bad, [b'ellis-1'])

def test_enroll_directory_reads_consented_photo_folders(tmp_path):
    (tmp_path / 'Ellis').mkdir(); (tmp_path / 'Morgan').mkdir(); (tmp_path / 'Empty').mkdir()
    (tmp_path / 'Ellis' / 'a.jpg').write_bytes(b'ellis-1')
    (tmp_path / 'Ellis' / 'b.jpeg').write_bytes(b'ellis-2')
    (tmp_path / 'Morgan' / 'a.jpg').write_bytes(b'morgan-1')
    (tmp_path / 'README.md').write_text('consent rules')
    idx = index()
    assert enroll_directory(idx, tmp_path) == {'Ellis': 2, 'Empty': 0, 'Morgan': 1}
    assert idx.names() == ['Ellis', 'Morgan']

def test_tracker_without_face_index_keeps_its_original_track_shape():
    t = PersonTracker(predictor=lambda jpeg: [upright_det(4)])
    assert 'identity' not in t.update(b'ellis-live', now_ms=0)[0]

def test_tracker_attaches_identity_retries_every_tenth_update_and_sticks():
    calls = []
    class FakeFaceIndex:
        def identify(self, jpeg, box):
            calls.append((jpeg, list(box)))
            return {'name': 'Ellis', 'score': 0.81234} if jpeg == b'face-visible' else None
    t = PersonTracker(predictor=lambda jpeg: [upright_det(4), upright_det(None)], face_index=FakeFaceIndex())
    first = t.update(b'back-turned', now_ms=0)
    assert first[0]['identity'] is None and first[1]['identity'] is None and len(calls) == 1
    for i in range(1, 10):  # updates 2..10 of track 4: no new face lookups
        assert t.update(b'face-visible', now_ms=i * 100)[0]['identity'] is None
    assert len(calls) == 1
    hit = t.update(b'face-visible', now_ms=1000)  # 11th update: second attempt
    assert hit[0]['identity'] == {'name': 'Ellis', 'score': 0.8123} and len(calls) == 2
    assert calls[1] == (b'face-visible', [90.0, 30.0, 150.0, 260.0])
    for i in range(11, 40):  # sticky: stays known with no further lookups
        assert t.update(b'back-turned', now_ms=i * 100)[0]['identity']['name'] == 'Ellis'
    assert len(calls) == 2
    assert hit[1]['identity'] is None  # untracked detections are never looked up

def test_tracker_forgets_identity_with_the_stale_track_and_survives_face_errors():
    class BrokenFaceIndex:
        def identify(self, jpeg, box):
            raise FaceIdError('backend missing')
    t = PersonTracker(predictor=lambda jpeg: [upright_det(4)], face_index=BrokenFaceIndex())
    out = t.update(b'frame', now_ms=0)
    assert out[0]['identity'] is None and out[0]['posture'] == 'upright'
    assert 'backend missing' in t.face_id_error

    frames = iter([[upright_det(4)], [], [upright_det(4)]])
    class OnceFaceIndex:
        def __init__(self): self.n = 0
        def identify(self, jpeg, box):
            self.n += 1
            return {'name': 'Ellis', 'score': 0.9} if self.n == 1 else None
    t = PersonTracker(predictor=lambda jpeg: next(frames), face_index=OnceFaceIndex())
    assert t.update(b'f', now_ms=0)[0]['identity']['name'] == 'Ellis'
    assert t.update(b'f', now_ms=5000) == []  # track 4 goes stale and is dropped
    assert t.update(b'f', now_ms=5100)[0]['identity'] is None  # a reused id is looked up afresh
