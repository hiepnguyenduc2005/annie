"""A historical prelude must retain the exact camera evidence identity."""
from uuid import uuid4
import pytest

from robot.simulation.phone_memory_prelude import validate_result


def captured():
    frame = {'frame_id': str(uuid4()), 'ts': 1000,
             'pose': {'x': -3.3, 'y': -4.35, 'yaw': 1.3, 'map_id': 'current-house'}}
    result = {'perception': {**frame, 'source': 'simulation_vlm', 'person': False,
              'posture': 'unknown', 'location': 'unknown', 'confidence': .9,
              'caption': 'A phone leans on the chair.', 'model': 'test-image-model'}}
    return frame, result


def test_prelude_preserves_caption_and_observer_pose_without_inventing_object_coordinates():
    frame, result = captured()
    observation = validate_result(frame, result)
    assert observation.pose.model_dump() == frame['pose']
    assert str(observation.frame_id) == frame['frame_id']
    assert observation.ts == frame['ts']
    assert observation.caption == result['perception']['caption']


@pytest.mark.parametrize('field,value', [('frame_id', str(uuid4())), ('ts', 1001),
    ('pose', {'x': 0., 'y': 0., 'yaw': 0., 'map_id': 'other-house'}),
    ('source', 'simulation_ground_truth')])
def test_changed_capture_or_authored_label_cannot_be_indexed_as_vision(field, value):
    frame, result = captured()
    result['perception'][field] = value
    with pytest.raises(ValueError):
        validate_result(frame, result)
