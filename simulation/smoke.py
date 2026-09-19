#!/usr/bin/env python3
"""Headless direct-MuJoCo physics smoke harness.

This harness exercises MuJoCo physics ONLY. It does not import or start any
DimOS module, does not download assets, and does not open network connections.
It answers one question: does a given MJCF model step stably for N steps on
this machine, and where does its free base end up?

Actuation: controls are held at zero (unactuated). When the model defines
keyframes, the simulation starts from the first keyframe; otherwise it starts
from mj_forward on the default configuration. This is NOT a gait test and NOT
a DimOS integration test.

Usage:
  python simulation/smoke.py --model path/to/model.xml --steps 200
  python simulation/smoke.py --model model.xml --steps 500 --json out.json
  python simulation/smoke.py --steps 200            # built-in minimal model

Exit code 0 = physics stepped for the requested count without divergence.
Exit code 1 = load failure, invalid arguments, or physics divergence.
Divergence (non-finite state, base below the abort floor, or a MuJoCo
numerical warning) is reported, never silently passed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

MINIMAL_XML = """
<mujoco>
  <worldbody>
    <geom name="floor" type="plane" size="5 5 0.1"/>
    <body name="box" pos="0 0 1">
      <freejoint/>
      <geom type="box" size="0.1 0.1 0.1" mass="1"/>
    </body>
  </worldbody>
</mujoco>
"""

# If a free base falls below this z, the model has fallen through the world.
ABORT_FLOOR_Z = -1.0


def check_warnings(mujoco, data) -> list[str]:
    """Return names of MuJoCo numerical warnings that fired during stepping."""
    names = {
        mujoco.mjtWarning.mjWARN_BADQPOS: "badqpos",
        mujoco.mjtWarning.mjWARN_BADQVEL: "badqvel",
        mujoco.mjtWarning.mjWARN_BADQACC: "badqacc",
    }
    fired = []
    for warn_enum, name in names.items():
        if data.warning[int(warn_enum)].number > 0:
            fired.append(name)
    return fired


def run(model_path: Path | None, steps: int, render_dir: Path | None) -> dict:
    import mujoco
    import numpy as np

    # from_xml_path (not from_xml_string) so relative asset paths such as
    # meshdir/file references resolve against the model file's directory.
    if model_path is not None:
        model = mujoco.MjModel.from_xml_path(str(model_path))
        model_label = str(model_path)
    else:
        model = mujoco.MjModel.from_xml_string(MINIMAL_XML)
        model_label = "<built-in minimal drop>"
    data = mujoco.MjData(model)

    # Keyframe-initialize when available; otherwise settle from defaults.
    if model.nkey > 0:
        mujoco.mj_resetDataKeyframe(model, data, 0)
    mujoco.mj_forward(model, data)

    base_qpos_adr = None
    for j in range(model.njnt):
        if int(model.jnt_type[j]) == int(mujoco.mjtJoint.mjJNT_FREE):
            base_qpos_adr = int(model.jnt_qposadr[j])
            break
    has_free_base = base_qpos_adr is not None

    initial_z = float(data.qpos[base_qpos_adr + 2]) if has_free_base else None
    final_z = initial_z

    renderer = None
    frame_count = 0
    try:
        if render_dir is not None:
            render_dir.mkdir(parents=True, exist_ok=True)
            renderer = mujoco.Renderer(model, height=240, width=320)

        # Controls are held at zero (unactuated): this is a passive physics
        # smoke test, not a gait or policy test.
        if model.nu > 0:
            data.ctrl[:] = 0.0

        wall_start = time.perf_counter()
        diverged = False
        divergence_reason = None
        steps_done = 0
        for step in range(steps):
            mujoco.mj_step(model, data)
            steps_done = step + 1
            warnings_fired = check_warnings(mujoco, data)
            if warnings_fired:
                diverged = True
                divergence_reason = f"mujoco warnings: {', '.join(warnings_fired)}"
                break
            if not (np.all(np.isfinite(data.qpos)) and np.all(np.isfinite(data.qvel))):
                diverged = True
                divergence_reason = "non-finite qpos or qvel"
                break
            if not np.isfinite(data.time):
                diverged = True
                divergence_reason = "non-finite simulation time"
                break
            if has_free_base:
                final_z = float(data.qpos[base_qpos_adr + 2])
                if final_z < ABORT_FLOOR_Z:
                    diverged = True
                    divergence_reason = f"base fell below z={ABORT_FLOOR_Z}"
                    break
            if renderer is not None and step % max(1, steps // 8) == 0:
                renderer.update_scene(data)
                frame = renderer.render()
                out = render_dir / f"frame_{step:05d}.png"
                try:
                    from PIL import Image

                    Image.fromarray(frame).save(out)
                except ImportError:
                    # No Pillow: write a portable pixmap instead.
                    h, w = frame.shape[:2]
                    ppm = render_dir / f"frame_{step:05d}.ppm"
                    with open(ppm, "wb") as f:
                        f.write(f"P6 {w} {h} 255\n".encode() + frame.tobytes())
                frame_count += 1

        wall_elapsed = time.perf_counter() - wall_start
    finally:
        # Always release the onscreen/GL context the renderer holds.
        if renderer is not None:
            renderer.close()

    return {
        "model": model_label,
        "mujoco_version": mujoco.__version__,
        "steps_requested": steps,
        "steps_completed": steps_done,
        "nq": int(model.nq),
        "nv": int(model.nv),
        "nu": int(model.nu),
        "nbody": int(model.nbody),
        "has_keyframe": bool(model.nkey > 0),
        "timestep": float(model.opt.timestep),
        "sim_seconds": float(model.opt.timestep) * steps_done,
        "wall_seconds": round(wall_elapsed, 3),
        "frames_written": frame_count,
        "has_free_base": has_free_base,
        "initial_base_z": initial_z,
        "final_base_z": final_z,
        "diverged": diverged,
        "divergence_reason": divergence_reason,
        "ok": not diverged,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="Path to a local MJCF .xml file. Omit to use the built-in minimal model.",
    )
    parser.add_argument("--steps", type=int, default=200, help="Physics steps to run.")
    parser.add_argument(
        "--render", type=Path, default=None, help="Directory for periodic PNG frames."
    )
    parser.add_argument("--json", type=Path, default=None, help="Write result JSON here.")
    args = parser.parse_args()

    if args.steps <= 0:
        print(f"ERROR: --steps must be a positive integer, got {args.steps}", file=sys.stderr)
        return 1
    if args.model is not None and not args.model.exists():
        print(f"ERROR: model not found: {args.model}", file=sys.stderr)
        return 1

    try:
        result = run(args.model, args.steps, args.render)
    except Exception as exc:  # noqa: BLE001 - report and fail cleanly
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    import json

    line = json.dumps(result, indent=2)
    print(line)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(line + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
