"""Camera and layer controls are validated and never change robot state."""
from types import SimpleNamespace

import pytest
from robot.simulation.viewer import MujocoSession, validate_control


@pytest.mark.parametrize('point', [None, [], [1,2], [1,2,3,4], [True,0,0], [float('nan'),0,0], [0,float('inf'),0], [101,0,0], '0,0,0'])
def test_pan_rejects_invalid_world_coordinates(point):
    with pytest.raises(ValueError):
        validate_control({'action':'camera', 'lookat':point})


def test_pan_orbit_and_zoom_leave_physics_untouched():
    command = validate_control({'action':'camera', 'lookat':[2,-3,1.2],
                                'azimuth':-170, 'elevation':-35, 'distance':12})
    session = MujocoSession.__new__(MujocoSession)
    session.camera = SimpleNamespace(azimuth=90, elevation=-20, distance=3, lookat=[0,0,0])
    session.track_robot = True
    session.running = True
    session.set_camera(command)
    assert session.camera.lookat == [2,-3,1.2]
    assert (session.camera.azimuth, session.camera.elevation, session.camera.distance) == (-170,-35,12)
    assert session.track_robot is False
    assert session.running is True


def test_room_preset_restores_camera_without_resetting_simulation():
    session = MujocoSession.__new__(MujocoSession)
    session.camera = SimpleNamespace(azimuth=90, elevation=-20, distance=3, lookat=[0,0,0])
    session.scene = {'overview':{'lookat':[0,0,1], 'azimuth':120,'elevation':-40,'distance':20}}
    session.track_robot = True
    session.running = True
    session.set_camera(validate_control({'action':'camera','preset':'room'}))
    assert session.camera.lookat == [0,0,1]
    assert session.camera.distance == 20
    assert not session.track_robot
    assert session.running


@pytest.mark.parametrize('payload', [{}, {'lidar':1}, {'route':'yes'}, {'trajectory':None}, {'unknown':True}, {'lidar':True,'preset':'room'}])
def test_layer_controls_reject_non_boolean_or_unknown_fields(payload):
    with pytest.raises(ValueError):
        validate_control({'action':'overlays', **payload})


def test_layer_controls_accept_independent_toggles():
    assert validate_control({'action':'overlays','lidar':False}) == {'action':'overlays','lidar':False}
    assert validate_control({'action':'overlays','lidar':True,'trajectory':False,'route':True})['route']
