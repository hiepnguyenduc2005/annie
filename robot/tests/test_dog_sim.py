"""SimDog: the simulated Go2 that stands in for the hardware behind the patrol's connection interface.

No robot and no network. Needs the dimOS venv (MuJoCo, OpenCV, the pose checkpoint) and the built
apartment scene (`robot/simulation/apartment.py --fetch --build`); skipped when those are missing.
"""
import asyncio
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "robot"))

np = pytest.importorskip("numpy")
pytest.importorskip("mujoco")
pytest.importorskip("cv2")

from robot.simulation import apartment  # noqa: E402

if not (apartment.OUT_DIR / "apartment_seated.xml").is_file() or not apartment.MANIFEST.is_file():
    pytest.skip("apartment scene not built (apartment.py --fetch --build)", allow_module_level=True)

from robot.dog.planning.smart_patrol import body_frame, sector_ranges, voxel_points_world  # noqa: E402
from robot.dog.runtime import patrol  # noqa: E402
from robot.dog.sim import SimDog  # noqa: E402
from robot.dog.sim import dog as sim  # noqa: E402

EAST = (-2.6, -1.25, 0.0)      # open floor, facing the kitchen run 5.4 m away
AT_WALL = (2.0, -1.25, 0.0)    # 0.87 m from the kitchen cabinet fronts (x = 2.87)


@pytest.fixture(scope="module")
def seated():
    return SimDog("seated", render=False)


def ranges_of(dog):
    msg = dog.voxel_message()["data"]
    pts = body_frame(voxel_points_world(msg["data"], msg), (dog.x, dog.y), dog.yaw)
    return sector_ranges(pts, ground_hint=sim.POSE_Z["stand"] - patrol.BASE_HEIGHT_M)


def test_topics_match_the_runtime():
    assert (sim.TOPIC_SPORT, sim.TOPIC_LOWSTATE, sim.TOPIC_POSE) == (patrol.TOPIC_SPORT, patrol.TOPIC_LOWSTATE, patrol.TOPIC_POSE)
    assert (sim.TOPIC_VOXELS, sim.TOPIC_SPORT_STATE) == (patrol.TOPIC_VOXELS, patrol.TOPIC_SPORT_STATE)
    assert (sim.STOP_MOVE, sim.MOVE, sim.EULER) == (patrol.STOP_MOVE, patrol.MOVE, patrol.EULER)


def test_move_walks_a_metre_then_the_wall_stops_it(seated):
    dog = seated
    dog.teleport(*EAST)
    for _ in range(40):  # 4 s at 0.3 m/s, refreshed every tick like the patrol does
        dog.command(vx=0.3)
        dog.step(0.1)
    assert 1.0 <= dog.x - EAST[0] <= 1.25 and abs(dog.y - EAST[1]) < 0.02
    pose = dog.pose_message()["data"]["pose"]
    assert patrol.extract_pose(dog.pose_message())["frame_id"] == "odom"
    assert pose["orientation"]["w"] == pytest.approx(1.0) and pose["position"]["x"] == pytest.approx(dog.x)

    for _ in range(300):
        dog.command(vx=0.3)
        dog.step(0.1)
    nose = dog.x + sim.BODY_LENGTH_M / 2
    assert 2.80 <= nose <= 2.88, f"nose must rest at the cabinet fronts (x = 2.87), got {nose:.3f}"
    stuck = dog.x
    for _ in range(20):
        dog.command(vx=0.3)
        dog.step(0.1)
    assert dog.x == stuck and dog.contacts >= 1, "no odometry progress at contact: the stall detector's cue"
    for _ in range(15):
        dog.command(vx=-0.2)
        dog.step(0.1)
    assert dog.x < stuck - 0.15, "reversing out of contact must work"


def test_a_stale_move_stops_and_yaw_reaches_the_pose_quaternion(seated):
    dog = seated
    dog.teleport(*EAST)
    dog.command(wz=0.5)
    for _ in range(10):
        dog.command(wz=0.5)
        dog.step(0.1)
    assert 0.3 < dog.yaw < 0.55
    parsed = patrol.extract_pose(dog.pose_message())
    assert patrol.quaternion_yaw(parsed["orientation_xyzw"]) == pytest.approx(dog.yaw, abs=1e-6)
    for _ in range(30):  # no refresh: the Move times out after 1 s and the body settles
        dog.step(0.1)
    yaw = dog.yaw
    dog.step(0.1)
    assert dog.yaw == yaw and not dog.vel.any()


def test_voxel_map_shows_the_wall_ahead_in_the_driver_layout(seated):
    dog = seated
    dog.teleport(*AT_WALL)
    msg = dog.voxel_message()["data"]
    positions = msg["data"]["positions"]
    assert positions.dtype == np.uint8 and positions.size % 12 == 0 and positions.size == msg["data"]["face_count"] * 12
    assert msg["resolution"] == 0.05 and max(msg["width"]) <= 255
    ox, oy, _ = msg["origin"]
    assert ox < dog.x < ox + msg["width"][0] * 0.05 and oy < dog.y < oy + msg["width"][1] * 0.05
    ranges = ranges_of(dog)
    assert ranges["front"] == pytest.approx(0.87, abs=0.08) and ranges["front"] < 1.2
    assert ranges["ground_z"] == pytest.approx(0.0, abs=0.03)

    dog.teleport(*EAST)
    assert ranges_of(dog)["front"] > 3.0  # open floor ahead


def test_people_move_in_the_map_and_block_the_body(seated):
    dog = seated
    dog.teleport(*EAST)
    start = dict(dog.people["visitor"])
    try:
        dog.set_person_pose("visitor", -1.6, -1.25, 180.0, "standing")  # one metre ahead, facing the dog
        assert ranges_of(dog)["front"] == pytest.approx(1.0, abs=0.2)
        for _ in range(80):
            dog.command(vx=0.3)
            dog.step(0.1)
        assert dog.x + sim.BODY_LENGTH_M / 2 < -1.6, "the body must stop at the person, not walk through"
        dog.teleport(*EAST)
        dog.set_person_pose("visitor", -1.6, -1.25, 90.0, "lying")
        lying = dog.model.geom_pos[dog.geometry.people_geoms["visitor"]]
        assert lying[:, 2].max() < 0.35, "a lying person is on the floor"
        dog.set_person_pose("visitor", 0.0, 0.0, 0.0, "absent")
        assert ranges_of(dog)["front"] > 3.0
        with pytest.raises(KeyError):
            dog.set_person_pose("nobody", 0.0, 0.0, 0.0)
    finally:
        dog.set_person_pose("visitor", start["x"], start["y"], start["yaw_deg"], start["posture"])


def test_floor_variant_has_a_lying_person_and_scenarios_lay_people_down():
    floor = SimDog("floor", render=False)
    geoms = floor.geometry.people_geoms["jeanine"]
    assert floor.people["jeanine"]["posture"] == "lying" and floor.model.geom_pos[geoms][:, 2].max() < 0.35

    dog = SimDog("seated", render=False, scenario="1.0:jeanine:lying:-1.3,0.1,8")
    head = dog.model.geom_pos[dog.geometry.people_geoms["jeanine"]][:, 2].max()
    assert head > 1.0  # seated on the couch
    for _ in range(12):
        dog.step(0.1)
    assert dog.people["jeanine"]["posture"] == "lying"
    assert dog.model.geom_pos[dog.geometry.people_geoms["jeanine"]][:, 2].max() < 0.35

    calls = []
    hooked = SimDog("empty", render=False, scenario=lambda d, t: calls.append(round(t, 1)))
    hooked.step(0.1)
    assert calls == [0.1] and hooked.people == {}


def test_sport_api_codes_and_tricks_hold_the_body(seated):
    dog = seated
    dog.teleport(*EAST)

    async def scenario():
        ask = dog.datachannel.pub_sub.publish_request_new
        codes = {}
        for name, api in (("stand", 1004), ("balance", 1002), ("stop", 1003), ("backflip", 1044)):
            dog.busy_until = 0.0
            codes[name] = patrol._status_code(await ask(sim.TOPIC_SPORT, {"api_id": api}))
        dog.busy_until = 0.0
        codes["hello"] = patrol._status_code(await ask(sim.TOPIC_SPORT, {"api_id": patrol.HELLO}))
        codes["dance_while_busy"] = patrol._status_code(await ask(sim.TOPIC_SPORT, {"api_id": 1022}))
        switcher = await ask(sim.TOPIC_MOTION_SWITCHER, {"api_id": 1001})
        return codes, switcher

    codes, switcher = asyncio.run(scenario())
    assert codes == {"stand": 0, "balance": 0, "stop": 0, "backflip": 3203, "hello": 0, "dance_while_busy": -1}
    assert patrol._status_code(switcher) == 0 and json.loads(switcher["data"]["data"])["name"] == "mcf"

    x = dog.x
    for _ in range(25):  # Hello holds the body for 3 s: Move is ignored
        dog.command(vx=0.3)
        dog.step(0.1)
    assert dog.busy and dog.x == x
    for _ in range(20):
        dog.command(vx=0.3)
        dog.step(0.1)
    assert not dog.busy and dog.x > x + 0.2


def test_lowstate_drains_slowly_from_88(seated):
    dog = SimDog("empty", render=False)
    low = patrol.extract_lowstate(dog.lowstate_message())
    assert low["battery"]["soc_percent"] == 88
    for _ in range(600):
        dog.step(1.0)
    assert 87.0 < dog.soc < 87.9
    dog.set_battery(35)
    assert patrol.extract_lowstate(dog.lowstate_message())["battery"]["soc_percent"] == 35


def test_rendered_frame_is_bgr_and_the_person_tracker_finds_someone(seated):
    import cv2
    dog = seated
    dog.teleport(*sim.DEFAULT_START)
    frame = dog.render_frame()
    img = frame.to_ndarray(format="bgr24")
    assert img.shape == (480, 640, 3) and img.dtype == np.uint8 and 40 < img.mean() < 220
    small = cv2.resize(img, (480, 360), interpolation=cv2.INTER_AREA)  # what patrol's convert() hands the tracker
    assert small.shape == (360, 480, 3)
    red = (small[:, :, 2].astype(int) - small[:, :, 0] > 120).mean()
    blue = (small[:, :, 0].astype(int) - small[:, :, 2] > 120).mean()
    assert red > 0.004 > blue, "Jeanine's red top must come through as red: the frame is BGR, not RGB"
    try:
        tracker = patrol._fast_tracker()
    except Exception as exc:  # no ultralytics / no pose checkpoint on this machine
        pytest.skip(f"person tracker unavailable: {type(exc).__name__}")
    tracks = []
    for k in range(3):
        tracks = tracker.update(small, now_ms=1000 + 100 * k)
    people = [t for t in tracks if t["conf"] >= 0.45]
    assert people, "the pose tracker must find a resident in the seated variant"
    assert all(t["posture"] != "lying" for t in people)


class NobodyTracker:
    def update(self, img, now_ms):
        return []


def test_three_second_patrol_runs_on_the_sim_and_reports_simulation():
    dog = SimDog("seated", rate_hz=10, camera_hz=10)
    report = asyncio.run(patrol.run_patrol_greet(
        ip="127.0.0.1", aes_key=None, conn_factory=dog.connect_factory(), source="simulation", duration_s=3.0,
        tracker=NobodyTracker(), speak=lambda text: None, status=lambda text: None, frontier_planner=None, idle_trick_s=0.0))
    assert report["source"] == "simulation"
    assert report["reason"] == "duration_complete" and report["completed"]
    assert report["battery_soc_start"] == 88
    assert report["lidar"]["maps"] > 0 and report["lidar"]["first_ranges"]["front"] is not None
    assert report["frames"] >= 10, "rendered camera frames must reach the perception loop"
    assert report["moves_sent"] > 0 and dog.travelled > 0.2, "the patrol's Move commands must move the simulated body"
    assert report["stop"]["code"] == 0 and dog.requests[-1][1]["api_id"] == patrol.STOP_MOVE
    assert dog.disconnected and dog.stats["render_ms"] is not None
    assert math.isfinite(report["max_distance_from_origin_m"]) and report["max_distance_from_origin_m"] < 3.0
