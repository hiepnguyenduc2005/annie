import base64
from uuid import uuid4
import pytest

from robot.simulation.evidence import save_evidence


def sample():
    frame = {'frame_id': str(uuid4()), 'ts': 1000, 'pose': {'map_id': 'house'},
             'source': 'simulation_render', 'jpeg_b64': base64.b64encode(b'\xff\xd8test').decode()}
    perception = {k:v for k,v in frame.items() if k != 'jpeg_b64'}
    perception['source'] = 'simulation_vlm'
    return frame, perception


def test_exact_original_image_is_retained_and_cache_is_bounded(tmp_path):
    for _ in range(3):
        frame, perception = sample()
        path = save_evidence(tmp_path, frame, perception, limit=2)
    assert len(list(tmp_path.glob('*.jpg'))) == len(list(tmp_path.glob('*.json'))) == 2
    assert open(path, 'rb').read() == b'\xff\xd8test'


@pytest.mark.parametrize('changed', ['source', 'frame_id', 'ts', 'pose'])
def test_unmatched_or_real_camera_data_is_not_cached(tmp_path, changed):
    frame, perception = sample()
    frame[changed] = {'source': 'hardware', 'frame_id': str(uuid4()), 'ts': 999,
                      'pose': {'map_id': 'different'}}[changed]
    with pytest.raises(ValueError):
        save_evidence(tmp_path, frame, perception)
    assert not list(tmp_path.iterdir())
