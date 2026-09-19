"""Local trained Go1 locomotion, using real MuJoCo joint actuation.

Observation/action layout adapted from Dimensional Inc.'s Apache-2.0
``dimos/simulation/mujoco/policy.py``. See LOCOMOTION.md for provenance.
This is a Go1 simulation surrogate, not a validated Go2 controller.
"""

from __future__ import annotations

import copy
import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / ".cache/dimos/data/mujoco_sim/unitree_go1.xml"
POLICY_PATH = ROOT / ".cache/dimos/data/mujoco_sim/unitree_go1_policy.onnx"
MESH_PATH = ROOT / ".cache/menagerie_full/unitree_go1/assets"
MODEL_LABEL = "Unitree Go1 locomotion surrogate (DimOS policy; not Go2 hardware)"


def prepare_locomotion_model(input_path: Path) -> Path:
    """Compose the matching Go1 model with an Annie Go2 scene's environment.

    Copies static environment and visual assets, never the Go2 dynamics or
    actuators. Output lives in ignored cache; source scene remains untouched.
    """
    input_path = Path(input_path).resolve()
    scene = ET.parse(input_path).getroot()
    robot = ET.parse(MODEL_PATH).getroot()
    if not MESH_PATH.is_dir():
        raise FileNotFoundError(f"Missing Go1 meshes: {MESH_PATH}")
    robot.set("model", "annie_go1_locomotion_surrogate")
    ET.SubElement(robot, "compiler", angle="radian", meshdir=str(MESH_PATH))
    compiler = scene.find("compiler")
    # Resolve scene asset paths before the robot compiler changes meshdir.
    for asset in scene.findall("asset/*"):
        if not asset.get("name", "").startswith("env_"):
            continue
        node = copy.deepcopy(asset)
        if node.get("file"):
            base = input_path.parent
            attr = "meshdir" if node.tag == "mesh" else "texturedir"
            if compiler is not None and compiler.get(attr):
                base = base / compiler.get(attr)
            node.set("file", str((base / node.get("file")).resolve()))
        robot.find("asset").append(node)
    for tag in ("visual", "statistic"):
        if scene.find(tag) is not None:
            robot.append(copy.deepcopy(scene.find(tag)))
    world = robot.find("worldbody")
    ET.SubElement(
        world.find("body[@name='trunk']"),
        "camera",
        name="robot_front",
        pos=".32 0 .12",
        xyaxes="0 -1 0 0 0 1",
        fovy="85",
    )
    for node in scene.findall("worldbody/*"):
        if node.get("name", "").startswith("env_"):
            world.append(copy.deepcopy(node))
    # Plain Go2 scene.xml includes the robot rather than embedding it, and has
    # no authored env_ floor. Supply the same simple support plane in that case.
    if not any(g.get("type") == "plane" for g in world.findall("geom")):
        ET.SubElement(
            world,
            "geom",
            name="env_floor",
            type="plane",
            size="20 20 .1",
            rgba=".65 .62 .56 1",
        )
    source_home = scene.find("keyframe/key[@name='home']")
    if source_home is not None:
        initial = source_home.get("qpos", "").split()
        if len(initial) >= 7:
            for key in robot.findall("keyframe/key"):
                pose = key.get("qpos").split()
                pose[:2], pose[3:7] = initial[:2], initial[3:7]
                key.set("qpos", " ".join(pose))
    xml = ET.tostring(robot, encoding="unicode")
    digest = hashlib.sha256(xml.encode()).hexdigest()[:20]
    target = ROOT / ".cache/locomotion" / f"{digest}.xml"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(xml)
    return target


class LocomotionController:
    """50 Hz policy, 200 Hz physics; command units m/s, m/s, rad/s.

    Call apply BEFORE every mj_step. No global MuJoCo callback is installed.
    Only data.ctrl changes: pose/displacement comes from physics, not teleporting.
    """

    def __init__(self, model, data, policy_path: Path | None = None):
        import numpy as np
        import onnxruntime as ort

        self.np = np
        self.model, self.data = model, data
        if (model.nq, model.nv, model.nu) != (19, 18, 12):
            raise ValueError("Go1 policy requires 19 qpos, 18 qvel and 12 actuators")
        expected = [
            f"{leg}_{joint}"
            for leg in ("FR", "FL", "RR", "RL")
            for joint in ("hip", "thigh", "calf")
        ]
        if [model.actuator(i).name for i in range(12)] != expected:
            raise ValueError("Policy requires matching Go1 actuator ordering")
        self.imu_id = model.site("imu").id
        self.base_id = model.body("trunk").id
        data.sensor("local_linvel")
        data.sensor("gyro")
        if not np.allclose(model.actuator_gainprm[:, 0], 35):
            raise ValueError("Policy requires matching Go1 position actuators")
        self.default = model.keyframe("home").qpos[7:].copy()
        model.opt.timestep = 0.005
        path = Path(policy_path or POLICY_PATH)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.policy = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        if self.policy.get_inputs()[0].shape[-1] != 48:
            raise ValueError("Expected the 48-observation DimOS Go1 policy")
        self.policy_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.reset()

    def reset(self):
        """Clear policy history after the caller resets MuJoCo to home."""
        self.last_action = self.np.zeros(12, dtype=self.np.float32)
        self.command = self.np.zeros(3, dtype=self.np.float32)
        self.counter = 0
        self.data.ctrl[:] = self.default

    def apply(self, vx: float, vy: float, wz: float):
        """Apply a bounded body-frame velocity command; zero means active stand.

        Zero velocity is a software stop request with settling time, never a
        hardware emergency stop. Caller must pause simulation on raised faults.
        """
        values = (vx, vy, wz)
        limits = (0.6, 0.3, 1.0)
        if any(
            isinstance(v, bool) or not math.isfinite(v) or abs(v) > limit
            for v, limit in zip(values, limits)
        ):
            raise ValueError("Velocity must be finite and within ±(.6,.3,1.0)")
        self.command[:] = values
        if self.counter % 4 == 0:
            np, d = self.np, self.data
            if not np.isfinite(d.qpos).all() or not np.isfinite(d.qvel).all():
                raise ValueError("Non-finite physics state")
            obs = np.hstack(
                (
                    d.sensor("local_linvel").data,
                    d.sensor("gyro").data,
                    d.site_xmat[self.imu_id].reshape(3, 3).T @ np.array([0, 0, -1]),
                    d.qpos[7:] - self.default,
                    d.qvel[6:],
                    self.last_action,
                    self.command,
                )
            ).astype(np.float32)
            action = self.policy.run(
                ["continuous_actions"], {"obs": obs.reshape(1, -1)}
            )[0][0]
            if action.shape != (12,) or not np.isfinite(action).all():
                raise ValueError("Invalid policy output")
            self.last_action = action.copy()
            d.ctrl[:] = action * 0.5 + self.default
        self.counter += 1

    def state(self):
        return {
            "model": MODEL_LABEL,
            "controller": "DimOS Go1 ONNX joint-position policy",
            "policy_sha256": self.policy_sha256,
            "command_body_mps_radps": self.command.tolist(),
            "physics_dt_s": 0.005,
            "policy_dt_s": 0.02,
            "hardware_connected": False,
        }
