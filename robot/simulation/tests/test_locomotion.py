"""Opt-in real physics checks: run with the cached DimOS Python environment."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from robot.simulation.locomotion import LocomotionController, prepare_locomotion_model


class LocomotionPhysicsTests(unittest.TestCase):
    def test_forward_turn_stop(self):
        import mujoco
        import numpy as np
        from robot.simulation.scenes import make_scene
        xml, _ = make_scene(ROOT / '.cache/menagerie/unitree_go2', 'empty', 0, 2026)
        with tempfile.TemporaryDirectory() as temp:
            scene = Path(temp) / 'scene.xml'
            scene.write_text(xml)
            model = mujoco.MjModel.from_xml_path(str(prepare_locomotion_model(scene)))
        data = mujoco.MjData(model)
        mujoco.mj_resetDataKeyframe(model, data, 0)
        mujoco.mj_forward(model, data)
        controller = LocomotionController(model, data)
        results = []
        for label, command in [('settle', (0, 0, 0)), ('forward', (.4, 0, 0)),
                               ('stop', (0, 0, 0)), ('turn', (0, 0, .5)), ('stop_after_turn', (0, 0, 0))]:
            start = data.qpos[:3].copy()
            angles, speeds, heights = [], [], []
            for _ in range(800):
                controller.apply(*command)
                mujoco.mj_step(model, data)
                self.assertTrue(np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all())
                self.assertTrue(all(w.number == 0 for w in data.warning))
                matrix = data.xmat[controller.base_id].reshape(3, 3)
                self.assertGreater(matrix[2, 2], .85)
                angles.append(float(np.arctan2(matrix[1, 0], matrix[0, 0])))
                speeds.append(float(np.linalg.norm(data.qvel[:2])))
                heights.append(float(data.qpos[2]))
            yaw = np.unwrap(angles)
            row = {'phase': label, 'command': command, 'duration_s': 4,
                   'displacement_m': (data.qpos[:3] - start).tolist(),
                   'yaw_delta_rad': float(yaw[-1] - yaw[0]),
                   'final_1s_mean_speed_mps': float(np.mean(speeds[-200:])),
                   'minimum_base_height_m': min(heights)}
            results.append(row)
            if label == 'forward':
                self.assertGreater(row['displacement_m'][0], 1.0)
                self.assertLess(row['displacement_m'][0], 2.0)
            if label.startswith('stop'):
                self.assertLess(row['final_1s_mean_speed_mps'], .02)
                self.assertLess(np.linalg.norm(row['displacement_m'][:2]), .15)
            if label == 'turn':
                self.assertGreater(row['yaw_delta_rad'], .4)
            self.assertGreater(min(heights), .25)
        for bad in [(float('nan'), 0, 0), (2, 0, 0), (0, 0, float('inf'))]:
            with self.assertRaises(ValueError):
                controller.apply(*bad)
        controller.reset()
        np.testing.assert_array_equal(controller.last_action, np.zeros(12))
        report = {'mujoco_version': mujoco.__version__, 'scene': 'furnished empty seed 2026',
                  'provenance': controller.state(), 'results': results}
        output = ROOT / '.cache/locomotion/validation.json'
        output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    unittest.main()
