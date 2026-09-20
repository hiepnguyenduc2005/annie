"""Which microphone and speaker Annie uses on the host, selectable by name at runtime.

The dog process runs on the Mac that holds the dog link, so "Annie's voice" is whatever audio device that Mac
uses: AirPods, the built-in mic/speakers, an iPhone over Continuity. This module lists them, resolves a
name to a sounddevice index, records from the chosen input and plays through the chosen output. No device
selected = the system default (afplay / default input), exactly the old behaviour.

A remote audio service (the whiteboard's "Audio I/O app" on a DGX) would implement the same two calls,
`recorder()` and `play()`, over HTTP; nothing here assumes the host is the only option. The first such device
is the "Annie Audio" iPhone app (`speaker/mic/`): select the name "iPhone (Annie Audio)" as input and/or output
and `phone_audio.PhoneAudioServer` (WebSocket, port ANNIE_PHONE_AUDIO_PORT, default 8030) carries the audio.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

RATE = 16000
PHONE_DEVICE = "iPhone (Annie Audio)"  # the speaker/mic iPhone app over WebSocket (phone_audio.py): mic AND speaker
PHONE_INDEX = -1000                    # never a PortAudio index; marks the virtual entry in list_devices()

_PHONE = None  # the process-wide PhoneAudioServer, created on first selection
_PHONE_LOCK = threading.Lock()


def is_phone(name: str | None) -> bool:
    """True for the phone app's device name (exact, or anything containing "annie audio"; case-insensitive)."""
    want = (name or "").strip().lower()
    return want == PHONE_DEVICE.lower() or "annie audio" in want


def phone_server(start: bool = False):
    """The shared PhoneAudioServer, or None when it was never selected. `start=True` creates and starts it
    (ANNIE_PHONE_AUDIO_PORT, default 8030; ANNIE_PHONE_AUDIO_HOST, default 0.0.0.0 so the phone can reach it)."""
    global _PHONE
    if not start:
        return _PHONE
    with _PHONE_LOCK:
        if _PHONE is None:
            from robot.dog.voice.phone_audio import DEFAULT_PORT, PhoneAudioServer
            try:
                port = int(os.environ.get("ANNIE_PHONE_AUDIO_PORT") or DEFAULT_PORT)
            except ValueError:
                port = DEFAULT_PORT
            _PHONE = PhoneAudioServer(host=os.environ.get("ANNIE_PHONE_AUDIO_HOST") or "0.0.0.0", port=port)
        server = _PHONE
    return server.start()


def _phone_status() -> dict:
    server = _PHONE
    if server is not None:
        return server.status()
    return {"listening": False, "connected": False, "peer": None, "rx_packets": 0, "tx_bytes": 0,
            "port": None, "path": None, "error": None}


def list_devices() -> list[dict]:
    """[{index, name, input: bool, output: bool}] from PortAudio (empty when sounddevice is missing), plus the
    virtual phone entry once its server exists (selected or started)."""
    out = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0 and d.get("max_output_channels", 0) <= 0:
                continue
            out.append({"index": i, "name": str(d.get("name", "")), "input": d.get("max_input_channels", 0) > 0,
                        "output": d.get("max_output_channels", 0) > 0})
    except Exception:
        out = []
    if _PHONE is not None:
        out.append({"index": PHONE_INDEX, "name": PHONE_DEVICE, "input": True, "output": True, "virtual": True})
    return out


def resolve(name: str | None, *, kind: str, devices: list[dict] | None = None) -> int | None:
    """Device index for a (case-insensitive, substring) name of the given kind ("input"/"output"); None = default
    or not found. The phone app's name resolves to PHONE_INDEX whether or not its server is up yet."""
    if not name or not name.strip():
        return None
    if is_phone(name):
        return PHONE_INDEX
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
        self.muted = False
        self._generation = 0
        self._outputs = {}
        self._start_phone_if_selected()

    def _start_phone_if_selected(self):
        """The phone app can only connect once its server is listening, so selecting it starts the server."""
        if is_phone(self.input_name) or is_phone(self.output_name):
            try:
                phone_server(start=True)
            except Exception:
                pass  # e.g. no websockets package: status()["phone"] stays "not listening"

    def configure(self, *, input_name=None, output_name=None) -> dict:
        with self._lock:
            if input_name is not None:
                self.input_name = input_name.strip() or None
            if output_name is not None:
                self.output_name = output_name.strip() or None
            self.version += 1
        self._start_phone_if_selected()
        return self.status()

    def status(self) -> dict:
        devs = list_devices()
        return {"input": self.input_name or "system default", "output": self.output_name or "system default",
                "input_index": resolve(self.input_name, kind="input", devices=devs),
                "output_index": resolve(self.output_name, kind="output", devices=devs),
                "devices": devs, "phone": _phone_status(), "muted": self.muted,
                "phone_playback_stop_supported": False}

    def recorder(self):
        """int16 mono chunk generator from the chosen input (the same shape the wake-word listener expects)."""
        idx = resolve(self.input_name, kind="input")
        if idx == PHONE_INDEX:
            return phone_server(start=True).recorder()
        from robot.simulation.live_listener import sounddevice_recorder
        return sounddevice_recorder(device=idx)

    def set_muted(self, muted: bool):
        """Suppress new playback and stop only output handles owned by this instance.

        Phone audio has no stop command; bytes already sent may finish playing.
        Microphone capture and the phone connection remain available.
        """
        with self._lock:
            self.muted = bool(muted)
            if self.muted:
                self._generation += 1
                for output, is_process in list(self._outputs.items()):
                    try:
                        if is_process:
                            output.terminate()
                        else:
                            output.abort()
                    except Exception:
                        pass

    def play(self, data: bytes, suffix=".mp3", timeout_s=60.0) -> bool:
        return self.play_cancellable(data, suffix=suffix, timeout_s=timeout_s)

    def play_cancellable(self, data: bytes, suffix=".mp3", timeout_s=60.0, *, cancelled=None) -> bool:
        """Play using owned, interruptible output; never stop unrelated host audio."""
        with self._lock:
            generation = self._generation
        def stopped():
            return self.muted or generation != self._generation or bool(cancelled and cancelled())
        if stopped():
            return False
        idx = resolve(self.output_name, kind="output")
        if idx == PHONE_INDEX:
            try:
                if stopped():
                    return False
                played = phone_server(start=True).play(data, suffix=suffix, timeout_s=timeout_s)
                return bool(played) and not stopped()
            except Exception:
                return False
        path = None
        output = None
        process = False
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                fh.write(data)
                path = fh.name
            if idx is None:
                with self._lock:
                    if stopped():
                        return False
                    output = subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    process = True
                    self._outputs[output] = process
                deadline = time.monotonic() + timeout_s
                while output.poll() is None:
                    if stopped() or time.monotonic() >= deadline:
                        output.terminate()
                        return False
                    time.sleep(0.02)
                return output.returncode == 0 and not stopped()
            wav = path + ".wav"
            rc = subprocess.run(["afconvert", path, wav, "-d", "LEI16", "-f", "WAVE"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout_s).returncode
            if rc != 0 or stopped():
                return False
            import soundfile as sf
            import sounddevice as sd
            audio, rate = sf.read(wav, dtype="int16", always_2d=True)
            with self._lock:
                if stopped():
                    return False
                output = sd.OutputStream(samplerate=rate, device=idx, channels=audio.shape[1], dtype="int16")
                self._outputs[output] = process
                output.start()
            deadline = time.monotonic() + timeout_s
            for offset in range(0, len(audio), 1024):
                if stopped() or time.monotonic() >= deadline:
                    return False
                output.write(audio[offset:offset + 1024])
            output.stop()
            return not stopped()
        except Exception:
            return False
        finally:
            if output is not None:
                with self._lock:
                    self._outputs.pop(output, None)
                try:
                    if process:
                        if output.poll() is None:
                            output.terminate()
                        try:
                            output.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            output.kill()
                            output.wait(timeout=0.5)
                    else:
                        try:
                            output.abort()
                        finally:
                            output.close()
                except Exception:
                    pass  # Device disappearance must not bypass temporary-file cleanup.
            if path:
                Path(path).unlink(missing_ok=True)
                Path(path + ".wav").unlink(missing_ok=True)


_DEVICES: AudioDevices | None = None


def shared() -> AudioDevices:
    global _DEVICES
    if _DEVICES is None:
        _DEVICES = AudioDevices()
    return _DEVICES
