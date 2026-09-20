import json

import numpy as np
import pytest

mujoco = pytest.importorskip('mujoco')
from robot.simulation.spatial import SpatialSensor


def scene(box=True):
    obstacle = '<geom name="near" type="box" pos="2 0 1.18" size=".1 .3 .3"/><geom name="far" type="box" pos="4 0 1.18" size=".1 .3 .3"/>' if box else ''
    model = mujoco.MjModel.from_xml_string(f'''<mujoco><worldbody>{obstacle}
      <body name="robot" pos="0 0 1"><freejoint/><geom type="sphere" size=".3"/>
      <body name="leg" pos=".7 0 .18"><geom type="sphere" size=".1"/></body>
      </body></worldbody></mujoco>''')
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return model, data


def test_nearest_surface_robot_subtree_exclusion_and_no_physics_mutation():
    model, data = scene()
    groups = model.geom_group.copy()
    qpos = data.qpos.copy()
    sensor = SpatialSensor(model)
    assert sensor.update(data, now=0)
    frame = sensor.snapshot()
    # Third elevation row is horizontal; first azimuth is world +X.
    ray = 2 * 144
    assert frame['ranges_m'][ray] == pytest.approx(1.9)
    hit = frame['ray_indices'].index(ray)
    assert frame['points_world'][hit] == pytest.approx([1.9, 0, 1.18])
    assert frame['hit_geom_ids'][hit] == model.geom('near').id
    assert model.geom('far').id not in frame['hit_geom_ids']
    np.testing.assert_array_equal(groups, model.geom_group)
    np.testing.assert_array_equal(qpos, data.qpos)
    json.dumps(frame, allow_nan=False)


def test_no_hits_are_null_not_fabricated_max_range_points():
    model, data = scene(box=False)
    sensor = SpatialSensor(model)
    sensor.update(data, now=0)
    frame = sensor.snapshot()
    assert frame['hit_count'] == 0
    assert frame['points_world'] == []
    assert frame['ranges_m'] == [None] * 720


def test_bounded_history_rate_limit_time_reset_and_snapshot_independence():
    model, data = scene()
    sensor = SpatialSensor(model, history_size=3)
    for i in range(6):
        data.qpos[1] = i * .1
        data.time = i
        mujoco.mj_forward(model, data)
        assert sensor.update(data, now=i)
        assert not sensor.update(data, now=i + .1)
    frame = sensor.snapshot()
    assert len(frame['trajectory_world']) == 3
    assert frame['trajectory_world'][0][1] == pytest.approx(.3)
    frame['points_world'].clear()
    assert sensor.snapshot()['points_world']
    data.time = 0
    assert sensor.update(data, now=5.1)
    assert len(sensor.snapshot()['trajectory_world']) == 1
    sensor.reset()
    assert sensor.snapshot() is None


def test_overlay_respects_capacity_and_flags():
    model, data = scene()
    sensor = SpatialSensor(model)
    sensor.update(data, now=0)
    overlay = mujoco.MjvScene(model, maxgeom=2)
    assert sensor.draw(overlay, lidar=False, trajectory=False) == 0
    assert sensor.draw(overlay) == 2
    assert overlay.ngeom == 2
    assert sensor.draw(overlay) == 0


def test_trajectory_and_planned_route_are_distinct_ground_overlays():
    model, data = scene()
    sensor = SpatialSensor(model)
    sensor.update(data, now=0)
    data.qpos[0] = .2
    mujoco.mj_forward(model, data)
    sensor.update(data, now=1)
    overlay = mujoco.MjvScene(model, maxgeom=5)
    assert sensor.draw(overlay, lidar=False, route=[(0, 0), (1, 0)]) == 2
    assert overlay.geoms[0].pos[2] == pytest.approx(.06)
    assert overlay.geoms[1].pos[2] == pytest.approx(.05)
    assert overlay.geoms[0].rgba[:3] == pytest.approx([1, .65, .12])
    assert overlay.geoms[1].rgba[:3] == pytest.approx([.2, 1, .35])
    assert sensor.snapshot()['trajectory_world'][-1][2] == pytest.approx(1)
