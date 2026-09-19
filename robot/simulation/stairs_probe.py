"""Measure the cached DimOS Go1 policy on real MuJoCo stairs.

Builds a supported ground floor plus three ascending steps (separate runs for
each rise), drives the robot at realistic low speeds, and reports measured
success only when the feet actually reach the top landing (ascent) or the
ground floor (descent). Physics is 200 Hz and the policy runs at 50 Hz through
robot.simulation.locomotion.LocomotionController; the base pose is never
written, so any displacement is physical.

Run from the repository root with the cached environment:

    .cache/dimos/.venv/bin/python robot/simulation/stairs_probe.py

Writes a machine-readable summary to .cache/locomotion/stairs_probe.json.
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import mujoco
import numpy as np

from robot.simulation.locomotion import LocomotionController, MODEL_PATH, MESH_PATH

RESULTS_PATH = Path(".cache/locomotion/stairs_probe.json")
FOOT_SITES = ("FR", "FL", "RR", "RL")


def build_stairs_xml(rise_m: float, tread_m: float, width_m: float) -> str:
    """Compose the Go1 model with a ground floor and a 3-step staircase."""
    robot = ET.parse(MODEL_PATH).getroot()
    robot.set("model", f"go1_stairs_{int(rise_m * 1000)}mm")
    ET.SubElement(robot, "compiler", angle="radian", meshdir=str(MESH_PATH))
    world = robot.find("worldbody")

    def slab(name, x0, x1, top_z):
        center_x, half_x = (x0 + x1) / 2, (x1 - x0) / 2
        half_z = max(top_z / 2, 0.05)
        ET.SubElement(
            world,
            "geom",
            name=name,
            type="box",
            size=f"{half_x} {width_m / 2} {half_z}",
            pos=f"{center_x} 0 {top_z - half_z}",
            rgba=".7 .68 .6 1",
            friction="1.0 0.02 0.001",
        )

    slab("ground", -3.0, 0.0, 0.0)
    for i in range(1, 4):
        slab(f"step{i}", (i - 1) * tread_m, i * tread_m, i * rise_m)
    slab("landing", 3 * tread_m, 3 * tread_m + 2.0, 3 * rise_m)
    return ET.tostring(robot, encoding="unicode")


def run_scenario(
    rise_m, descend, speed_mps, duration_s, tread_m=0.30, width_m=1.2,
    speed_schedule=None,
):
    """Run one stair scenario and return measured results."""
    xml = build_stairs_xml(rise_m, tread_m, width_m)
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, 0)
    home = model.keyframe("home").qpos
    top_z = 3 * rise_m
    if descend:
        data.qpos[0] = 3 * tread_m + 0.8
        data.qpos[2] = top_z + home[2]
    else:
        data.qpos[0] = -0.6
        data.qpos[2] = home[2]
    data.qpos[3:7] = home[3:7]
    data.qpos[7:] = home[7:]
    mujoco.mj_forward(model, data)

    controller = LocomotionController(model, data)
    controller.reset()
    foot_ids = [model.site(name).id for name in FOOT_SITES]
    vx = -speed_mps if descend else speed_mps

    steps = int(round(duration_s / model.opt.timestep))
    min_z, max_z = math.inf, -math.inf
    samples = []
    warning = None
    start_x = float(data.qpos[0])
    success_hold_steps = 0
    required_hold = int(1.0 / model.opt.timestep)
    first_reach_step = None
    schedule = speed_schedule or [(duration_s, speed_mps)]

    for step in range(steps):
        base_x, base_z = float(data.qpos[0]), float(data.qpos[2])
        foot_z = [float(data.site_xpos[i][2]) for i in foot_ids]
        upright = float(data.qpos[3]) > 0.85
        if descend:
            on_floor = (
                base_x < -0.3
                and 0.2 < base_z < 0.4
                and max(foot_z) < 0.06
            )
        else:
            on_floor = (
                base_x > 3 * tread_m + 0.15
                and base_z > top_z + 0.15
                and min(foot_z) > top_z - 0.03
            )
        finite = bool(np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all())
        if on_floor and upright and finite:
            success_hold_steps += 1
            if first_reach_step is None:
                first_reach_step = step
        else:
            success_hold_steps = 0

        t = step * model.opt.timestep
        if success_hold_steps >= required_hold:
            cmd = (0.0, 0.0, 0.0)
        else:
            seg_speed = speed_mps
            acc = 0.0
            for seg_dur, seg_v in schedule:
                acc += seg_dur
                if t < acc:
                    seg_v_signed = -seg_v if descend else seg_v
                    seg_speed = seg_v_signed
                    break
            cmd = (seg_speed, 0.0, 0.0)
        controller.apply(*cmd)
        mujoco.mj_step(model, data)

        base_z = float(data.qpos[2])
        min_z, max_z = min(min_z, base_z), max(max_z, base_z)
        if step % 100 == 0:
            samples.append({
                "t_s": round(step * model.opt.timestep, 3),
                "base_x": round(float(data.qpos[0]), 4),
                "base_z": round(base_z, 4),
                "foot_z_max": round(max(foot_z), 4),
            })
        if not finite:
            warning = f"non-finite state at step {step}"
            break
        if base_z < 0.02:
            warning = f"base height {base_z:.3f} m at step {step}: fell"
            break
        if any(data.warning[i].number > 0 for i in range(mujoco.mjtWarning.mjNWARNING)):
            warning = f"MuJoCo warning at step {step}"
            break

    final_x, final_z = float(data.qpos[0]), float(data.qpos[2])
    final_foot_z = [round(float(data.site_xpos[i][2]), 4) for i in foot_ids]
    success = success_hold_steps >= required_hold
    return {
        "scenario": "descent" if descend else "ascent",
        "rise_m": rise_m,
        "tread_m": tread_m,
        "speed_cmd_mps": speed_mps,
        "speed_schedule": [
            [round(d, 2), v] for d, v in (speed_schedule or [[duration_s, speed_mps]])
        ],
        "duration_s": round(steps * model.opt.timestep, 2),
        "success": bool(success),
        "first_reach_t_s": (
            round(first_reach_step * model.opt.timestep, 2)
            if first_reach_step is not None else None
        ),
        "start_base_x": round(start_x, 4),
        "final_base_x": round(final_x, 4),
        "final_base_z": round(final_z, 4),
        "final_pitch_rad": round(
            float(np.arcsin(np.clip(2 * (data.qpos[3] * data.qpos[5] - data.qpos[4] * data.qpos[6]), -1, 1))), 3
        ),
        "final_foot_z": final_foot_z,
        "base_z_min": round(min_z, 4) if math.isfinite(min_z) else None,
        "base_z_max": round(max_z, 4) if math.isfinite(max_z) else None,
        "net_displacement_m": round(final_x - start_x, 4),
        "warning": warning,
        "state_written_only_through_ctrl": True,
        "samples": samples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rises", type=float, nargs="+", default=[0.05, 0.10, 0.17])
    parser.add_argument("--speeds", type=float, nargs="+", default=[0.15, 0.2, 0.25])
    parser.add_argument("--duration", type=float, default=25.0)
    parser.add_argument("--tread", type=float, default=0.30)
    args = parser.parse_args()

    results = []
    for rise in args.rises:
        best = {"ascent": None, "descent": None}
        for descend in (False, True):
            for speed in args.speeds:
                outcome = run_scenario(rise, descend, speed, args.duration, args.tread)
                print(
                    f"rise={rise:.2f} {outcome['scenario']} v={speed:.2f} -> "
                    f"success={outcome['success']} "
                    f"dx={outcome['net_displacement_m']:+.3f} "
                    f"z_final={outcome['final_base_z']:.3f} "
                    f"warn={outcome['warning']}",
                    flush=True,
                )
                results.append(outcome)
                key = "descent" if descend else "ascent"
                if outcome["success"]:
                    best[key] = speed
                    break
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "model": MODEL_PATH.name,
        "policy_sha256": "386cde6a1eac679e2f2a313ade2b395d9f26905b151ee3b019c2c4163d49b6f2",
        "physics_dt_s": 0.005,
        "policy_dt_s": 0.02,
        "tread_m": args.tread,
        "results": results,
    }
    RESULTS_PATH.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
