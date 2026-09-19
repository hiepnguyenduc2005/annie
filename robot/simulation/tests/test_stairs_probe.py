"""Opt-in probe validity checks: run with the cached DimOS Python environment."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from robot.simulation.stairs_probe import build_stairs_xml


class StairsGeometryTests(unittest.TestCase):
    """Compile the composed model and check physical stair geometry."""

    @classmethod
    def setUpClass(cls):
        import mujoco

        cls.mujoco = mujoco
        cls.model = mujoco.MjModel.from_xml_string(build_stairs_xml(0.17, 0.30, 1.2))

    def aabb(self, name):
        model = self.model
        gid = model.geom(name).id
        size = model.geom_size[gid]
        pos = model.geom_pos[gid]
        return pos - size, pos + size

    def test_model_compiles_with_expected_structure(self):
        self.assertEqual((self.model.nq, self.model.nv, self.model.nu), (19, 18, 12))
        for name in ("ground", "step1", "step2", "step3", "landing"):
            self.assertGreaterEqual(self.model.geom(name).id, 0)

    def test_step_heights_ascend_monotonically(self):
        tops = [self.aabb(f"step{i}")[1][2] for i in (1, 2, 3)]
        self.assertAlmostEqual(tops[0], 0.17, places=6)
        self.assertAlmostEqual(tops[1], 0.34, places=6)
        self.assertAlmostEqual(tops[2], 0.51, places=6)
        self.assertEqual(self.aabb("landing")[1][2], tops[2])

    def test_landing_is_flush_with_top_step(self):
        (_, ground_hi) = self.aabb("ground")
        self.assertAlmostEqual(ground_hi[2], 0.0, places=6)
        step_lo, step_hi = self.aabb("step3")
        land_lo, land_hi = self.aabb("landing")
        self.assertAlmostEqual(land_hi[2], step_hi[2], places=6)
        self.assertAlmostEqual(step_hi[0], land_lo[0], places=6)
        self.assertGreaterEqual(land_hi[0] - land_lo[0], 0.9)

    def test_floor_and_steps_overlap_in_width(self):
        for name in ("ground", "step1", "step2", "step3", "landing"):
            lo, hi = self.aabb(name)
            self.assertGreaterEqual(hi[1] - lo[1], 1.2)

    def test_robot_starts_on_ground_not_inside_stairs(self):
        import numpy as np

        # The probe places the trunk at x=-0.6 before stepping.
        data = self.mujoco.MjData(self.model)
        self.mujoco.mj_resetDataKeyframe(self.model, data, 0)
        self.mujoco.mj_forward(self.model, data)
        ground_lo, ground_hi = self.aabb("ground")
        step_lo, _ = self.aabb("step1")
        self.assertEqual(float(data.qpos[0]), 0.0)
        self.assertAlmostEqual(data.qpos[2], 0.35, places=6)
        self.assertGreater(ground_hi[0] - ground_lo[0], 2.0)
        trunk_x = float(data.qpos[0])
        self.assertLessEqual(trunk_x, step_lo[0] + 1e-9)
        self.assertTrue(np.isfinite(data.qpos).all())


if __name__ == "__main__":
    unittest.main()
