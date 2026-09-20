"""Compile the phone setup and verify its scale and collision isolation."""
from pathlib import Path

import numpy as np
import pytest

from robot.simulation.house import make_house

mujoco = pytest.importorskip('mujoco')
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def phone_house(tmp_path):
    assets = ROOT / '.cache/menagerie/unitree_go2'
    if not (assets / 'go2.xml').exists():
        pytest.skip('Pinned robot assets are not installed')
    xml, record = make_house(assets)
    path = tmp_path / 'house.xml'
    path.write_text(xml)
    model = mujoco.MjModel.from_xml_path(str(path))
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, 0)
    mujoco.mj_forward(model, data)
    return model, data, record


def test_phone_is_life_size_and_stays_inside_chair_footprint(phone_house):
    model, data, record = phone_house
    phone = model.body('env_phone')
    case = model.geom('env_phone_case')
    assert (2 * case.size) == pytest.approx([.076, .010, .158])
    assert int(phone.jntnum[0]) == 0
    assert (model.nq, model.nv, model.nu) == (19, 18, 12)
    assert record['ground_truth']['objects']['phone'] == pytest.approx(phone.pos)
    for geom_id in np.flatnonzero(model.geom_bodyid == phone.id):
        assert model.geom_contype[geom_id] == model.geom_conaffinity[geom_id] == 0
        rotation = data.geom_xmat[geom_id].reshape(3, 3)
        extent = np.abs(rotation) @ model.geom_size[geom_id]
        center = data.geom_xpos[geom_id]
        assert np.all(center[:2] - extent[:2] >= [-3.36, -3.65])
        assert np.all(center[:2] + extent[:2] <= [-2.64, -2.92])


def test_optional_camera_looks_at_phone_without_changing_home_pose(phone_house):
    model, data, record = phone_house
    camera = model.camera('phone_memory')
    assert int(camera.targetbodyid[0]) == model.body('env_phone').id
    rotation = data.cam_xmat[camera.id].reshape(3, 3)
    direction = data.xpos[model.body('env_phone').id] - data.cam_xpos[camera.id]
    direction /= np.linalg.norm(direction)
    assert -rotation[:, 2] == pytest.approx(direction, abs=1e-6)
    assert data.qpos[:3] == pytest.approx(record['ground_truth']['robot_home_position_m'])
    assert 25 <= float(camera.fovy[0]) <= 50
