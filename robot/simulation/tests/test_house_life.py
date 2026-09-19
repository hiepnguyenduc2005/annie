"""Real MuJoCo checks for home geometry, actor isolation and route connectivity."""
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
import pytest
mujoco=pytest.importorskip('mujoco')
from robot.simulation.house import make_house
from robot.simulation.locomotion import prepare_locomotion_model
from robot.simulation.daily_life import ResidentRoutine
from robot.simulation.navigation import Navigator
ROOT=Path(__file__).resolve().parents[3]

@pytest.fixture
def home(tmp_path):
    assets=ROOT/'.cache/menagerie/unitree_go2'
    if not assets.exists(): pytest.skip('Pinned robot assets are not installed')
    xml,record=make_house(assets)
    path=tmp_path/'home.xml';path.write_text(xml)
    model=mujoco.MjModel.from_xml_path(str(prepare_locomotion_model(path)))
    data=mujoco.MjData(model);mujoco.mj_resetDataKeyframe(model,data,0)
    routine=ResidentRoutine(model,data,record);mujoco.mj_forward(model,data)
    return model,data,routine,record,ET.fromstring(xml)

def test_physical_house_preserves_robot_and_has_flush_stairs(home):
    model,data,routine,record,xml=home
    assert (model.nq,model.nv,model.nu)==(19,18,12)
    assert model.nmocap==13
    assert len(record['rooms'])==16
    tops=[]
    for i in range(18):
        g=model.geom(f'env_stair_{i:02d}')
        tops.append(float(g.pos[2]+g.size[2]))
    assert tops==pytest.approx([(i+1)/6 for i in range(18)],abs=1e-6)
    landing=model.geom('env_upper_landing')
    assert landing.pos[2]+landing.size[2]==pytest.approx(3)
    assert landing.pos[1]-landing.size[1]==pytest.approx(5.4)
    assert not record['stairs']['traversal_verified']

def test_actor_moves_without_robot_pose_or_control_writes(home):
    model,data,routine,record,xml=home
    qpos=data.qpos.copy();ctrl=data.ctrl.copy()
    routine.update(15);first=data.mocap_pos.copy()
    routine.update(17);second=data.mocap_pos.copy()
    assert np.linalg.norm(first-second)>.2
    assert np.array_equal(data.qpos,qpos)
    assert np.array_equal(data.ctrl,ctrl)
    routine.update(15);assert np.allclose(first,data.mocap_pos)
    assert routine.state()['speed_mps']==.4

def test_fall_stays_at_actor_position_and_recovery_resumes(home):
    model,data,routine,record,xml=home
    routine.update(16);position=routine.position.copy()
    routine.trigger('fall');routine.update(18)
    assert routine.state()['posture']=='lying_floor'
    assert np.allclose(routine.position,position)
    routine.trigger('recover');routine.update(20)
    assert routine.state()['posture']=='walking'
    assert np.allclose(routine.position,position)

def test_every_ground_floor_waypoint_has_a_route(home):
    model,data,routine,record,xml=home
    nav=Navigator(model,data,record)
    assert len(nav.waypoints)==7
    for wp in nav.waypoints:
        assert nav.plan(tuple(data.qpos[:2]),(wp['x'],wp['y']))
