"""Opt-in real MuJoCo checks for the Navigator waypoint missions.

Run with the cached DimOS Python environment, for example:
    .cache/dimos/.venv/bin/python robot/simulation/tests/test_navigation.py

These tests drive the trained Go1 policy through actual physics (no renderer,
no pose writes) and measure waypoint completion, furniture contacts,
stop/resume, command-ID idempotency and queue bounds across all six scene
categories. The root venv has no MuJoCo; this file is standalone unittest.
"""
import json
from pathlib import Path
import math
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / ".data/simulation/scenes/manifest.json"
SCENE_IDS = [
    "safe_bed-000",
    "floor_lying-000",
    "seated-000",
    "standing-000",
    "occluded-000",
    "empty-000",
]
REPORT = ROOT / ".cache/locomotion/navigation_validation.json"

from robot.simulation.locomotion import LocomotionController, prepare_locomotion_model
from robot.simulation.navigation import Navigator


def load_scene_entry(scene_id):
    manifest = json.loads(MANIFEST.read_text())
    entry = next(s for s in manifest["scenes"] if s["id"] == scene_id)
    return entry, MANIFEST.parent / entry["file"]


def furniture_contacts(model, data):
    """Contacts with env_ furniture; walkable floor/rug surfaces are excluded."""
    hits = []
    for i in range(data.ncon):
        contact = data.contact[i]
        names = [model.geom(contact.geom1).name or "", model.geom(contact.geom2).name or ""]
        env = next((n for n in names if n.startswith("env_")), None)
        if env is None or "floor" in env or "rug" in env:
            continue
        hits.append({"env_geom": env, "depth_m": float(contact.dist)})
    return hits


class Rig:
    """A prepared scene model with controller and navigator, no renderer."""

    def __init__(self, scene_id):
        import mujoco

        self.mujoco = mujoco
        self.scene_id = scene_id
        self.scene, scene_path = load_scene_entry(scene_id)
        self.prepared = prepare_locomotion_model(scene_path)
        self.model = mujoco.MjModel.from_xml_path(str(self.prepared))
        self.reset()

    def reset(self):
        mujoco = self.mujoco
        self.data = mujoco.MjData(self.model)
        mujoco.mj_resetDataKeyframe(self.model, self.data, 0)
        mujoco.mj_forward(self.model, self.data)
        self.controller = LocomotionController(self.model, self.data)
        self.navigator = Navigator(self.model, self.data, self.scene)

    def resident_xy(self):
        origin = self.scene.get("ground_truth", {}).get("resident_origin_m")
        return tuple(origin[:2]) if origin else None

    def resident_footprints(self):
        """Raw (not robot-inflated) world XY boxes of resident geoms."""
        import numpy as np

        boxes = []
        for i in range(self.model.ngeom):
            name = self.model.geom(i).name or ""
            if not name.startswith(("env_person_", "env_resident")):
                continue
            rotation = self.data.geom_xmat[i].reshape(3, 3)
            if int(self.model.geom_type[i]) == 7:
                center = self.data.geom_xpos[i] + rotation @ self.model.geom_aabb[i][:3]
                half = np.abs(rotation) @ self.model.geom_aabb[i][3:]
            else:
                center = self.data.geom_xpos[i]
                half = np.abs(rotation) @ self.model.geom_size[i]
            boxes.append(
                (
                    float(center[0] - half[0]),
                    float(center[0] + half[0]),
                    float(center[1] - half[1]),
                    float(center[1] + half[1]),
                )
            )
        return boxes


def inside_any(point, boxes):
    x, y = point
    return any(a <= x <= b and c <= y <= d for a, b, c, d in boxes)


class NavigationPhysicsTests(unittest.TestCase):
    report = {"scenes": SCENE_IDS, "missions": []}

    @classmethod
    def tearDownClass(cls):
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(cls.report, indent=2) + "\n")
        print(json.dumps(cls.report, indent=2))

    def run_mission(self, rig, max_sim_s, label, assert_finite=True):
        """Step real physics while the Navigator commands the policy."""
        import numpy as np

        dt = rig.model.opt.timestep
        contacts = 0
        min_height = math.inf
        min_resident_dist = math.inf
        footprints = rig.resident_footprints()
        footprint_entries = 0
        resident = rig.resident_xy()
        for _ in range(int(max_sim_s / dt)):
            vx, vy, wz = rig.navigator.velocity()
            self.assertTrue(
                all(math.isfinite(v) for v in (vx, vy, wz)),
                f"{label}: Navigator returned a non-finite velocity",
            )
            rig.controller.apply(vx, vy, wz)
            rig.mujoco.mj_step(rig.model, rig.data)
            self.assertTrue(
                np.isfinite(rig.data.qpos).all() and np.isfinite(rig.data.qvel).all(),
                f"{label}: non-finite physics state at t={rig.data.time:.2f}s",
            )
            contacts += len(furniture_contacts(rig.model, rig.data))
            min_height = min(min_height, float(rig.data.qpos[2]))
            if inside_any(tuple(rig.data.qpos[:2]), footprints):
                footprint_entries += 1
            if resident is not None:
                min_resident_dist = min(
                    min_resident_dist,
                    math.dist(tuple(rig.data.qpos[:2]), resident),
                )
            if rig.navigator.state in ("idle", "failed"):
                break
        row = {
            "scene": rig.scene_id,
            "mission": label,
            "state": rig.navigator.state,
            "sim_time_s": float(rig.data.time),
            "furniture_contact_steps": contacts,
            "min_base_height_m": min_height,
            "resident_footprint_entries": footprint_entries,
            "min_resident_distance_m": None if resident is None else min_resident_dist,
            "final_xy_m": [float(v) for v in rig.data.qpos[:2]],
            "active_command": rig.navigator.active,
        }
        self.report["missions"].append(row)
        return row

    def test_goto_and_patrol_all_categories(self):
        for scene_id in SCENE_IDS:
            with self.subTest(scene=scene_id):
                rig = Rig(scene_id)
                nav = rig.navigator
                size = scene_size = rig.scene["ground_truth"]["room_size_m"]
                lo_x, hi_x, lo_y, hi_y = nav.bounds
                self.assertAlmostEqual(hi_x - lo_x, size[0] - 0.96, places=6)
                self.assertAlmostEqual(hi_y - lo_y, size[1] - 0.96, places=6)
                ids = {w["id"] for w in nav.waypoints}
                self.assertEqual(ids, {"home", "living-room", "bedroom", "hallway"})
                for w in nav.waypoints:
                    self.assertTrue(
                        lo_x <= w["x"] <= hi_x and lo_y <= w["y"] <= hi_y,
                        f"{scene_id} waypoint {w['id']} outside bounds",
                    )
                    self.assertTrue(nav.free((w["x"], w["y"])))
                self.assertGreater(len(nav.obstacles), 0, f"{scene_id} has no obstacles")
                # The start pose must not be inside the inflated planning map,
                # otherwise no route exists and missions would fail at issue.
                start = (float(rig.data.qpos[0]), float(rig.data.qpos[1]))
                self.assertTrue(
                    nav.free(start),
                    f"{scene_id} authored start lies inside an inflated obstacle; "
                    "the scene's home keyframe needs adjustment",
                )
                if scene_id != "empty-000":
                    footprints = rig.resident_footprints()
                    self.assertTrue(footprints, f"{scene_id} has no resident geoms")
                    for w in nav.waypoints:
                        self.assertFalse(
                            inside_any((w["x"], w["y"]), footprints),
                            f"{scene_id} waypoint {w['id']} inside resident footprint",
                        )

                for waypoint_id in ("living-room", "bedroom", "home"):
                    rig.reset()
                    target = rig.navigator.waypoints
                    w = next(x for x in target if x["id"] == waypoint_id)
                    rig.navigator.command("goto", waypoint=waypoint_id)
                    row = self.run_mission(rig, 120, f"goto {waypoint_id}")
                    self.assertEqual(row["state"], "idle", row["active_command"])
                    self.assertEqual(row["active_command"]["status"], "completed")
                    dist = math.dist(row["final_xy_m"], (w["x"], w["y"]))
                    self.assertLess(
                        dist, 0.25, f"{scene_id} stopped {dist:.3f} m from {waypoint_id}"
                    )
                    self.assertGreater(row["min_base_height_m"], 0.17)
                    self.assertEqual(row["furniture_contact_steps"], 0)
                    self.assertEqual(
                        row["resident_footprint_entries"],
                        0,
                        f"{scene_id} robot entered the resident footprint",
                    )
                    row["goal_distance_m"] = dist

                rig.reset()
                rig.navigator.command("patrol")
                targets = {w["id"]: (w["x"], w["y"]) for w in rig.navigator.waypoints}
                visits = {}
                footprints = rig.resident_footprints()
                footprint_entries = 0
                furniture_hits = 0
                dt = rig.model.opt.timestep
                import numpy as np

                for _ in range(int(150 / dt)):
                    rig.controller.apply(*rig.navigator.velocity())
                    rig.mujoco.mj_step(rig.model, rig.data)
                    self.assertTrue(np.isfinite(rig.data.qpos).all())
                    furniture_hits += len(furniture_contacts(rig.model, rig.data))
                    if inside_any(tuple(rig.data.qpos[:2]), footprints):
                        footprint_entries += 1
                    p = tuple(float(v) for v in rig.data.qpos[:2])
                    for name, goal in targets.items():
                        if name not in visits and math.dist(p, goal) < 0.3:
                            visits[name] = float(rig.data.time)
                    if rig.navigator.state in ("idle", "failed"):
                        break
                row = {
                    "scene": scene_id,
                    "mission": "patrol",
                    "state": rig.navigator.state,
                    "sim_time_s": float(rig.data.time),
                    "visits": visits,
                    "furniture_contact_steps": furniture_hits,
                    "resident_footprint_entries": footprint_entries,
                    "active_command": rig.navigator.active,
                }
                self.report["missions"].append(row)
                self.assertEqual(rig.navigator.state, "idle", rig.navigator.active)
                self.assertEqual(furniture_hits, 0, f"{scene_id} patrol hit furniture")
                self.assertEqual(
                    footprint_entries,
                    0,
                    f"{scene_id} patrol entered the resident footprint",
                )
                for name in targets:
                    self.assertIn(
                        name, visits, f"{scene_id} patrol missed {name}"
                    )

    def test_stop_settles_then_resume_completes(self):
        import numpy as np

        rig = Rig("safe_bed-000")
        rig.navigator.command("goto", waypoint="living-room")
        dt = rig.model.opt.timestep
        for _ in range(int(4 / dt)):
            rig.controller.apply(*rig.navigator.velocity())
            rig.mujoco.mj_step(rig.model, rig.data)
        self.assertEqual(rig.navigator.state, "moving")
        rig.navigator.command("stop")
        self.assertEqual(rig.navigator.state, "stopped")
        self.assertEqual(rig.navigator.velocity(), (0.0, 0.0, 0.0))
        for _ in range(int(4 / dt)):
            vx, vy, wz = rig.navigator.velocity()
            self.assertEqual((vx, vy, wz), (0.0, 0.0, 0.0))
            rig.controller.apply(vx, vy, wz)
            rig.mujoco.mj_step(rig.model, rig.data)
        self.assertLess(
            float(np.linalg.norm(rig.data.qvel[:2])),
            0.05,
            "robot did not settle after software stop",
        )
        rig.navigator.command("resume")
        row = self.run_mission(rig, 120, "stop/resume goto living-room")
        self.assertEqual(row["state"], "idle", row["active_command"])
        w = next(x for x in rig.navigator.waypoints if x["id"] == "living-room")
        self.assertLess(math.dist(row["final_xy_m"], (w["x"], w["y"])), 0.25)

    def test_command_ids_idempotent_and_queue_bounded(self):
        rig = Rig("safe_bed-000")
        nav = rig.navigator
        nav.command("goto", waypoint="living-room", command_id="cmd-1")
        before = list(nav.commands)
        nav.command("goto", waypoint="bedroom", command_id="cmd-1")
        self.assertEqual(nav.commands, before)
        self.assertEqual(
            len([c for c in nav.commands if c["command_id"] == "cmd-1"]), 1
        )
        nav.command("goto", waypoint="bedroom", command_id="cmd-2")
        superseded = next(c for c in nav.commands if c["command_id"] == "cmd-1")
        self.assertEqual(superseded["status"], "failed")
        self.assertEqual(nav.active["command_id"], "cmd-2")
        # Resume with no stored route fails the command instead of hanging.
        rig.reset()
        rig.navigator.command("resume", command_id="cmd-3")
        self.assertEqual(rig.navigator.state, "failed")
        self.assertEqual(rig.navigator.commands[0]["status"], "failed")
        # Queue stays bounded under a burst of distinct commands.
        for i in range(150):
            rig.navigator.command("stop", command_id=f"burst-{i}")
        self.assertLessEqual(len(rig.navigator.commands), 100)

    def test_turn_rotates_in_physics_until_measured_heading(self):
        import numpy as np

        rig = Rig("floor_lying-000")
        nav = rig.navigator
        dt = rig.model.opt.timestep

        def measured_yaw():
            w, x, y, z = rig.data.qpos[3:7]
            return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

        def angle(value):
            return (value + math.pi) % (2 * math.pi) - math.pi

        start_yaw = measured_yaw()
        # heading is an absolute world yaw; pick one distinct from the start.
        target_yaw = angle(-2.0) if abs(angle(start_yaw - -2.0)) > 0.2 else angle(2.0)
        start_p = tuple(float(v) for v in rig.data.qpos[:2])
        footprints = rig.resident_footprints()
        resident = rig.resident_xy()
        footprint_entries = 0
        furniture_hits = 0

        nav.command("turn", heading=target_yaw)
        self.assertEqual(nav.state, "turning")
        vx0, vy0, wz0 = nav.velocity()
        self.assertEqual((vx0, vy0), (0.0, 0.0), "turn must not translate")
        self.assertNotEqual(wz0, 0.0, "turn must command rotation immediately")
        self.assertLess(abs(angle(nav.turn_yaw - target_yaw)), 1e-9)
        max_yaw_rate = 0.0
        for _ in range(int(15 / dt)):
            vx, vy, wz = nav.velocity()
            self.assertTrue(all(math.isfinite(v) for v in (vx, vy, wz)))
            if nav.state == "turning":
                self.assertEqual((vx, vy), (0.0, 0.0), "turn must not translate")
                max_yaw_rate = max(max_yaw_rate, abs(wz))
            rig.controller.apply(vx, vy, wz)
            rig.mujoco.mj_step(rig.model, rig.data)
            furniture_hits += len(furniture_contacts(rig.model, rig.data))
            if inside_any(tuple(rig.data.qpos[:2]), footprints):
                footprint_entries += 1
            if nav.state != "turning":
                break
        self.assertEqual(nav.state, "idle", nav.active)
        self.assertEqual(nav.active["status"], "completed")
        error = abs(angle(measured_yaw() - target_yaw))
        self.assertLess(error, 0.12, f"final yaw error {error:.3f} rad")
        self.assertGreater(max_yaw_rate, 0.05, "turn never commanded rotation")
        self.assertEqual(
            furniture_hits, 0, "rotation mission must not touch furniture"
        )
        self.assertEqual(footprint_entries, 0, "rotation entered resident footprint")
        drift = math.dist(tuple(float(v) for v in rig.data.qpos[:2]), start_p)
        if resident is not None:
            self.assertGreater(
                math.dist(tuple(float(v) for v in rig.data.qpos[:2]), resident),
                0.3,
                "turn left the base next to the resident origin",
            )
        self.assertLess(drift, 0.35, f"turn drifted {drift:.3f} m from start")

    def test_turn_heading_zero_is_absolute_world_yaw(self):
        rig = Rig("floor_lying-000")
        nav = rig.navigator
        dt = rig.model.opt.timestep

        def measured_yaw():
            w, x, y, z = rig.data.qpos[3:7]
            return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

        def angle(value):
            return (value + math.pi) % (2 * math.pi) - math.pi

        # Turn to a non-zero world yaw first so the base is NOT at yaw 0.
        nav.command("turn", heading=1.2)
        for _ in range(int(15 / dt)):
            vx, vy, wz = nav.velocity()
            rig.controller.apply(vx, vy, wz)
            rig.mujoco.mj_step(rig.model, rig.data)
            if nav.state != "turning":
                break
        self.assertEqual(nav.state, "idle", nav.active)
        pre_yaw = measured_yaw()
        self.assertGreater(abs(angle(pre_yaw)), 0.9, "setup left yaw near zero")

        # heading=0 must rotate BACK to absolute world yaw zero, proving the
        # target is absolute rather than a relative offset from current yaw.
        nav.command("turn", heading=0)
        self.assertEqual(nav.state, "turning")
        self.assertEqual(nav.turn_yaw, 0.0)
        for _ in range(int(15 / dt)):
            vx, vy, wz = nav.velocity()
            self.assertEqual((vx, vy), (0.0, 0.0), "turn must not translate")
            rig.controller.apply(vx, vy, wz)
            rig.mujoco.mj_step(rig.model, rig.data)
            if nav.state != "turning":
                break
        self.assertEqual(nav.state, "idle", nav.active)
        self.assertEqual(nav.active["status"], "completed")
        final_error = abs(angle(measured_yaw()))
        self.assertLess(final_error, 0.12, f"heading=0 left yaw error {final_error:.3f}")

        # Range and type guards under the absolute contract.
        nav.command("turn", heading=math.pi + 0.01)
        self.assertEqual(nav.state, "failed")
        self.assertIn("[-pi, pi]", nav.active["detail"])

    def test_turn_rejects_bad_heading_and_stop_guard_applies_to_rotation(self):
        rig = Rig("safe_bed-000")
        nav = rig.navigator
        for bad in (None, "0.5", float("nan"), float("inf"), True):
            nav.command("turn", heading=bad)
            self.assertEqual(nav.state, "failed", repr(bad))
            self.assertEqual(nav.active["status"], "failed", repr(bad))
        # Person/furniture stop guard applies during turning: force the base
        # center into a raw collider and the turn must fail, not keep spinning.
        rig.reset()
        collider = rig.navigator.colliders[0]
        rig.data.qpos[0] = (collider[0] + collider[1]) / 2
        rig.data.qpos[1] = (collider[2] + collider[3]) / 2
        import mujoco

        mujoco.mj_forward(rig.model, rig.data)
        rig.navigator.command("turn", heading=1.0)
        self.assertEqual(rig.navigator.state, "turning")
        self.assertEqual(rig.navigator.velocity(), (0.0, 0.0, 0.0))
        self.assertEqual(rig.navigator.state, "failed")

    def test_navigator_without_scene_uses_default_room(self):
        rig = Rig("safe_bed-000")
        nav = Navigator(rig.model, rig.data, None)
        lo_x, hi_x, lo_y, hi_y = nav.bounds
        self.assertAlmostEqual(hi_x - lo_x, 6 - 0.96, places=6)
        self.assertAlmostEqual(hi_y - lo_y, 5 - 0.96, places=6)
        self.assertEqual(nav.scene, {})
        # Unknown waypoint fails explicitly rather than hanging.
        nav.command("goto", waypoint="kitchen")
        self.assertEqual(nav.state, "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
