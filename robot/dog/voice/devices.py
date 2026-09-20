"""Which microphone and speaker Annie uses on the host, selectable by name at runtime.

The dog process runs on the Mac that holds the dog link, so "Annie's voice" is whatever audio device that Mac
uses: AirPods, the built-in mic/speakers, an iPhone over Continuity. This module lists them, resolves a
name to a sounddevice index, records from the chosen input and plays through the chosen output. No device
selected = the system default (afplay / default input), exactly the old behaviour.

A remote audio service (the whiteboard's "Audio I/O app" on a DGX) would implement the same two calls,
`recorder()` and `play()`, over HTTP; nothing here assumes the host is the only option.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

RATE = 16000


def list_devices() -> list[dict]:
    """[{index, name, input: bool, output: bool}] from PortAudio; empty when sounddevice is missing."""
    try:
        import sounddevice as sd
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0 and d.get("max_output_channels", 0) <= 0:
                continue
            out.append({"index": i, "name": str(d.get("name", "")), "input": d.get("max_input_channels", 0) > 0,
                        "output": d.get("max_output_channels", 0) > 0})
        return out
    except Exception:
        return []


def resolve(name: str | None, *, kind: str, devices: list[dict] | None = None) -> int | None:
    """Device index for a (case-insensitive, substring) name of the given kind ("input"/"output"); None = default
    or not found."""
    if not name or not name.strip():
        return None
    want = name.strip().lower()
    devs = list_devices() if devices is None else devices
    exact = [d for d in devs if d.get(kind) and d["name"].lower() == want]
    part = [d for d in devs if d.get(kind) and want in d["name"].lower()]
    pick = (exact or part or [None])[0]
    return None if pick is None else int(pick["index"])


class AudioDevices:
    """Current input/output choice (names) with thread-safe updates; `version` bumps so listeners re-open."""

    def __init__(self, input_name: str | None = None, output_name: str | None = None):
        self.input_name = input_name or os.environ.get("ANNIE_AUDIO_INPUT") or None
        self.output_name = output_name or os.environ.get("ANNIE_AUDIO_OUTPUT") or None
        self.version = 0
        self._lock = threading.Lock()

    def configure(self, *, input_name=None, output_name=None) -> dict:
        with self._lock:
            if input_name is not None:
                self.input_name = input_name.strip() or None
            if output_name is not None:
                self.output_name = output_name.strip() or None
            self.version += 1
        return self.status()

    def status(self) -> dict:
        devs = list_devices()
        return {"input": self.input_name or "system default", "output": self.output_name or "system default",
                "input_index": resolve(self.input_name, kind="input", devices=devs),
                "output_index": resolve(self.output_name, kind="output", devices=devs),
                "devices": devs}

    def recorder(self):
        """int16 mono chunk generator from the chosen input (the same shape the wake-word listener expects)."""
        from robot.simulation.live_listener import sounddevice_recorder
        return sounddevice_recorder(device=resolve(self.input_name, kind="input"))

    def play(self, data: bytes, suffix=".mp3", timeout_s=60.0) -> bool:
        """Play encoded audio: afplay on the system default, or decode with afconvert and stream through the
        chosen output device with sounddevice."""
        idx = resolve(self.output_name, kind="output")
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                fh.write(data)
                path = fh.name
            if idx is None:
                rc = subprocess.run(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s).returncode
                return rc == 0
            wav = path + ".wav"
            rc = subprocess.run(["afconvert", path, wav, "-d", "LEI16", "-f", "WAVE"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout_s).returncode
            if rc != 0:
                return False
            import soundfile as sf
            import sounddevice as sd
            audio, rate = sf.read(wav, dtype="int16")
            sd.play(audio, rate, device=idx, blocking=True)
            Path(wav).unlink(missing_ok=True)
            return True
        except Exception:
            return False
        finally:
            if path:
                Path(path).unlink(missing_ok=True)


_DEVICES: AudioDevices | None = None


def shared() -> AudioDevices:
    global _DEVICES
    if _DEVICES is None:
        _DEVICES = AudioDevices()
    return _DEVICES
