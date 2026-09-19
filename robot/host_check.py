"""Inventory the local robot host without recording media or contacting services."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys


def command(args: list[str]) -> dict:
    if not shutil.which(args[0]):
        return {"status": "missing"}
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except OSError:
        return {"status": "failed"}
    # Only call inventory commands; never echo environment, account, or device keys.
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "output": result.stdout[:12000].strip(),
    }


def inspect_host() -> dict:
    packages = {}
    for name in ("unitree-webrtc-connect", "dimos", "av", "fastapi", "uvicorn"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None

    audio = {}
    if platform.system() == "Linux":
        audio = {
            "capture_devices": command(["arecord", "-l"]),
            "playback_devices": command(["aplay", "-l"]),
        }
    elif platform.system() == "Darwin":
        inventory = command(["system_profiler", "SPAudioDataType", "-json"])
        if inventory["status"] == "ok":
            try:
                devices = [
                    item
                    for group in json.loads(inventory["output"]).get("SPAudioDataType", [])
                    for item in group.get("_items", [])
                ]
                audio = {
                    "status": "ok",
                    "usb_devices": [
                        {
                            "name": item.get("_name"),
                            "input_channels": item.get("coreaudio_device_input", 0),
                            "output_channels": item.get("coreaudio_device_output", 0),
                        }
                        for item in devices
                        if item.get("coreaudio_device_transport") == "coreaudio_device_type_usb"
                    ],
                }
            except (ValueError, TypeError, AttributeError):
                audio = {"status": "unreadable"}
        else:
            audio = {"status": inventory["status"]}

    return {
        "source": "host_inventory",
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "python_3_12": sys.version_info[:2] == (3, 12),
        "packages": packages,
        "tools": {name: bool(shutil.which(name)) for name in ("uv", "ffmpeg", "ssh")},
        "gpu": command([
            "nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"
        ]),
        "audio": audio,
        "robot_connection": "not_tested",
        "local_vision": "not_tested",
        "audible_playback": "not_tested",
        "microphone_capture": "not_tested",
    }


if __name__ == "__main__":
    print(json.dumps(inspect_host(), indent=2))
