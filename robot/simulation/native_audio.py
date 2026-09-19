"""Native macOS audio playback for simulated dog speech.

Plays rendered WAV clips through the macOS `afplay` binary on the machine's
default output device, selected explicitly (no per-clip device choice). Used
when the viewer runs with `--native-audio`; a native worker thread keeps
playback ordered and bounded independently of browser autoplay settings.

Boundaries: this module reports when the playback process completed. It never
claims a human heard the clip. Callbacks carry process status and monotonic +
wall timestamps only; there is no fake audibility signal.
"""
from __future__ import annotations

import logging
import math
import queue
import subprocess
import threading
import time
from pathlib import Path

LOG = logging.getLogger("annie.simulation.native_audio")

DEFAULT_OUTPUT_DIR = Path(".data/simulation/speech")
DEFAULT_AFPLAY = "afplay"
MIN_DURATION_S = 0.05
MAX_DURATION_S = 30.0
MAX_FILE_BYTES = 5_000_000
QUEUE_SIZE = 8
DEFAULT_JOIN_TIMEOUT_S = 5.0


class NativeAudioError(Exception):
    """Raised for invalid clips or failed playback processes."""


def _require_number(value, name, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeAudioError(f"{name} must be a finite number.")
    if not math.isfinite(value):
        raise NativeAudioError(f"{name} must be a finite number.")
    if minimum is not None and value < minimum:
        raise NativeAudioError(f"{name} must be >= {minimum}.")
    if maximum is not None and value > maximum:
        raise NativeAudioError(f"{name} must be <= {maximum}.")
    return float(value)


class NativeAudioPlayer:
    """Bounded serial playback queue backed by `afplay` subprocesses.

    Two layers:

    * :meth:`play` is synchronous for worker threads that already own a
      thread; it blocks until the clip finishes or the hard timeout fires.
    * :meth:`enqueue` accepts bounded clip dicts for a serial worker
      thread, so callers (e.g. a viewer handler) never block on playback.

    At most one `afplay` process runs at a time, enforced by a global lock
    shared by both layers. Queue slots are bounded (default 8); when full,
    :meth:`enqueue` fails fast rather than growing without bound.
    """

    _process_lock = threading.Lock()  # global: one playback at a time

    def __init__(self, output_dir=DEFAULT_OUTPUT_DIR, *,
                 afplay_path=DEFAULT_AFPLAY, queue_size=QUEUE_SIZE,
                 max_file_bytes=MAX_FILE_BYTES):
        self.output_dir = Path(output_dir).resolve()
        self.afplay_path = afplay_path
        self.max_file_bytes = int(max_file_bytes)
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._worker = None
        self._started = False
        self._stop = threading.Event()

    # ---- validation ----

    def _resolve_clip(self, clip):
        if not isinstance(clip, dict):
            raise NativeAudioError("Clip must be a dict with file_path and duration_s.")
        file_path = clip.get("file_path")
        duration_s = clip.get("duration_s")
        if not isinstance(file_path, str) or not file_path:
            raise NativeAudioError("Clip file_path must be a non-empty string.")
        duration = _require_number(duration_s, "duration_s",
                                   minimum=MIN_DURATION_S, maximum=MAX_DURATION_S)
        path = Path(file_path)
        if not path.is_absolute():
            path = self.output_dir / path
        resolved = path.resolve()
        if resolved.suffix.lower() != ".wav":
            raise NativeAudioError("Clip must be a .wav file.")
        if not resolved.is_relative_to(self.output_dir):
            raise NativeAudioError(
                f"Clip must live under the output directory {self.output_dir}.")
        if not resolved.is_file():
            raise NativeAudioError(f"Clip file does not exist: {resolved.name}")
        size = resolved.stat().st_size
        if size > self.max_file_bytes:
            raise NativeAudioError(
                f"Clip exceeds {self.max_file_bytes} bytes (got {size}).")
        return resolved, duration

    # ---- synchronous layer ----

    def play(self, path, duration_s):
        """Play one clip synchronously; returns when the process completes.

        Raises NativeAudioError for invalid clips, a missing binary, nonzero
        exit, or timeout. The hard timeout is min(duration + 5, 35) seconds.
        Status strings describe the process, not human audibility.
        """
        clip = {"file_path": str(path), "duration_s": duration_s}
        resolved, duration = self._resolve_clip(clip)
        timeout_s = min(duration + 5.0, 35.0)
        started_monotonic = time.monotonic()
        started_wall = time.time()
        with self._process_lock:
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    [self.afplay_path, str(resolved)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise NativeAudioError(f"Cannot start audio player binary.") from exc
            try:
                returncode = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise NativeAudioError(
                    f"Playback timed out after {timeout_s:g}s.") from None
        if returncode != 0:
            raise NativeAudioError(f"Audio player exited with status {returncode}.")
        ended_monotonic = time.monotonic()
        ended_wall = time.time()
        return {
            "status": "playback_process_completed",
            "started_monotonic": started_monotonic,
            "started_wall": started_wall,
            "ended_monotonic": ended_monotonic,
            "ended_wall": ended_wall,
        }

    # ---- queued layer ----

    def start(self):
        """Start the single worker thread. Idempotent."""
        if self._started:
            return
        self._started = True
        self._worker = threading.Thread(
            target=self._run, name="native-audio-worker", daemon=True)
        self._worker.start()

    def enqueue(self, clip, *, on_state_callback=None):
        """Add one clip dict to the bounded queue; fails fast when full.

        `clip` needs `file_path` and `duration_s`. `on_state_callback`
        receives dicts with `state` in {"playing", "played", "failed"},
        monotonic + wall timestamps, and, on failure, a bounded `error`
        message. The callback runs on the worker thread; exceptions from it
        are logged and swallowed so one bad callback cannot kill the queue.
        """
        resolved, duration = self._resolve_clip(clip)
        try:
            self._queue.put_nowait((resolved, duration, on_state_callback))
        except queue.Full:
            raise NativeAudioError(
                "Playback queue is full; clip rejected.") from None

    def _emit(self, callback, state, error=None):
        if callback is None:
            return
        payload = {
            "state": state,
            "monotonic": time.monotonic(),
            "wall": time.time(),
        }
        if error is not None:
            payload["error"] = error
        try:
            callback(payload)
        except Exception:
            LOG.exception("Playback state callback raised; ignored.")

    def _run(self):
        while not self._stop.is_set():
            try:
                resolved, duration, callback = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._emit(callback, "playing")
                self.play(resolved, duration)
            except NativeAudioError as exc:
                self._emit(callback, "failed", error=str(exc))
            else:
                self._emit(callback, "played")
            finally:
                self._queue.task_done()

    def join(self, timeout=DEFAULT_JOIN_TIMEOUT_S):
        """Wait for the queue to drain (test/integration helper)."""
        self._queue.join()

    def stop(self):
        """Stop the worker after the current clip finishes."""
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=DEFAULT_JOIN_TIMEOUT_S)
