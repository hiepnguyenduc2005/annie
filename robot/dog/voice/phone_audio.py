"""The "Annie Audio" iPhone app (`speaker/mic/`) as a network microphone and speaker for the dog process.

The app opens ONE WebSocket to this server and both directions carry binary frames of raw PCM: signed 16-bit
little-endian, mono, 16 kHz, no header. The phone sends 20 ms packets (320 samples = 640 bytes); we may send
frames of any length and the phone schedules them back-to-back. Text frames are ignored. The phone pings
every 10 s (the `websockets` library answers with a pong) and we ping it too, so a dead link is noticed on
both sides. The phone must be told where we are: `AudioServerConfig.serverURL` in
`speaker/mic/SpeakerMic/AudioConnection.swift` = `ws://<this Mac's LAN IP>:8030/audio`.

`PhoneAudioServer` offers the same two calls as a host audio device (`robot/dog/voice/devices.py`):
`recorder()` yields int16 mono chunks shaped like `sounddevice_recorder`'s, and `play()` speaks encoded audio.
One phone at a time, the newest connection wins. With no phone connected the recorder yields silence (the
VAD loop keeps running) and `play()` returns False (the caller falls back to the host speaker).

Privacy: microphone audio from the phone lives only in bounded in-memory queues (about 2 s per open
recorder, oldest dropped first); it is never written to disk or logged. Speech to be PLAYED is decoded by
macOS `afconvert` through a temporary file that is deleted before `play()` returns, like the `afplay` path.
There is no authentication: anything on the LAN that reaches the port can be the phone, so use a trusted network.
"""
from __future__ import annotations

import asyncio
import collections
import http
import subprocess
import tempfile
import threading
import time
import wave
import weakref
from pathlib import Path

import numpy as np

RATE = 16000                # the app's wire format and robot.simulation.live_listener.RATE
CHUNK_SAMPLES = 512         # robot.simulation.live_listener.CHUNK_SAMPLES: the Silero VAD window (32 ms)
DEVICE_NAME = "iPhone (Annie Audio)"
DEFAULT_PORT = 8030
BYTES_PER_S = RATE * 2
MAX_QUEUED_S = 2.0          # per open recorder; beyond this the oldest audio is dropped instead of building latency
TX_FRAME_BYTES = 6400       # 200 ms per frame (<= 8 KB), always an even number of bytes
TX_LEAD_S = 0.4             # how far ahead of real time we send: the phone's jitter buffer
MAX_RX_FRAME_BYTES = 1 << 20


def decode_to_pcm16(data: bytes, suffix: str = ".mp3", timeout_s: float = 30.0) -> bytes | None:
    """Encoded audio (mp3/wav/aiff/m4a...) -> raw PCM16 mono 16 kHz via macOS `afconvert`; None on any failure.
    Both temporary files are deleted before returning."""
    src = wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(data)
            src = fh.name
        wav = src + ".16k.wav"
        rc = subprocess.run(["afconvert", src, wav, "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s).returncode
        if rc != 0:
            return None
        with wave.open(wav, "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
                return None
            return w.readframes(w.getnframes())
    except Exception:
        return None
    finally:
        for p in (src, wav):
            if p:
                Path(p).unlink(missing_ok=True)


class _Tap:
    """One open recorder's bounded queue of int16 chunks (every open recorder hears the whole stream)."""
    __slots__ = ("chunks", "samples")

    def __init__(self):
        self.chunks: collections.deque = collections.deque()
        self.samples = 0

    def put(self, chunk: np.ndarray, max_samples: int):
        chunk = chunk[-max_samples:]
        self.chunks.append(chunk)
        self.samples += chunk.size
        while self.samples > max_samples:
            self.samples -= self.chunks.popleft().size

    def take(self, n: int) -> np.ndarray | None:
        if self.samples < n:
            return None
        parts, need = [], n
        while need:
            head = self.chunks.popleft()
            if head.size > need:
                self.chunks.appendleft(head[need:])
                head = head[:need]
            parts.append(head)
            need -= head.size
        self.samples -= n
        return np.concatenate(parts).astype(np.int16, copy=False)


class PhoneAudioServer:
    """WebSocket endpoint for the phone app, in a daemon thread with its own asyncio loop."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT, path: str = "/audio", *, decoder=decode_to_pcm16):
        self.host, self.port, self.path = host, int(port), path
        self.decoder = decoder
        self._cond = threading.Condition()   # guards taps and the connection fields below
        self._taps: list[_Tap] = []
        self._ws = None
        self._peer: str | None = None
        self._rx_packets = 0
        self._tx_bytes = 0
        self._listening = False
        self._bound_port: int | None = None
        self._error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._tx_lock = threading.Lock()     # one utterance at a time; frames never interleave
        self._closing: set = set()

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, timeout_s: float = 5.0) -> "PhoneAudioServer":
        """Idempotent. Returns once the socket is bound (or binding failed: see status()["error"])."""
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._error = None
                self._thread = threading.Thread(target=self._run, daemon=True, name="phone-audio")
                self._thread.start()
        self._ready.wait(timeout_s)
        return self

    def stop(self, timeout_s: float = 5.0):
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass  # the loop already closed
        if thread is not None:
            thread.join(timeout_s)

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:  # port in use, no websockets package, ...
            self._error = type(exc).__name__
        finally:
            with self._cond:
                self._listening, self._ws, self._peer = False, None, None
                self._cond.notify_all()
            self._loop = None
            self._ready.set()
            loop.close()

    async def _serve(self):
        from websockets.asyncio.server import serve
        self._stop = asyncio.Event()
        async with serve(self._handler, self.host, self.port, process_request=self._check_path, compression=None,
                         max_size=MAX_RX_FRAME_BYTES, ping_interval=10, ping_timeout=20, close_timeout=2) as server:
            self._bound_port = server.sockets[0].getsockname()[1]
            self._listening = True
            self._ready.set()
            await self._stop.wait()

    def _check_path(self, connection, request):
        want = self.path.rstrip("/") or "/"
        got = request.path.split("?", 1)[0].rstrip("/") or "/"
        if got != want:
            return connection.respond(http.HTTPStatus.NOT_FOUND, f"audio lives at {want}\n")
        return None

    # ---- phone -> us -----------------------------------------------------------------------------------------
    async def _handler(self, ws):
        from websockets.exceptions import ConnectionClosed
        addr = ws.remote_address
        with self._cond:
            old, self._ws = self._ws, ws
            self._peer = str(addr[0]) if addr else None
            self._cond.notify_all()
        if old is not None:  # newest wins; close the old one without making the new phone wait for the handshake
            task = asyncio.ensure_future(old.close(1000, "replaced by a newer phone"))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        carry = b""
        try:
            async for message in ws:
                if isinstance(message, str):
                    continue  # text frames are not part of the protocol
                data = carry + message
                if len(data) % 2:
                    data, carry = data[:-1], data[-1:]
                else:
                    carry = b""
                if data:
                    self._ingest(np.frombuffer(data, dtype="<i2"))
        except ConnectionClosed:
            pass
        finally:
            with self._cond:
                if self._ws is ws:
                    self._ws, self._peer = None, None
                self._cond.notify_all()

    def _ingest(self, chunk: np.ndarray):
        limit = int(MAX_QUEUED_S * RATE)
        with self._cond:
            self._rx_packets += 1
            for tap in self._taps:
                tap.put(chunk, limit)
            self._cond.notify_all()

    def recorder(self, chunk: int = CHUNK_SAMPLES, timeout_s: float = 0.2):
        """Generator of int16 mono chunks of `chunk` samples, like `sounddevice_recorder`. It hears the phone
        from this call on. When a full chunk does not arrive in time (no phone, mic not started, stalled
        network) it yields silence, so a VAD loop keeps running; late audio is delivered afterwards, not lost."""
        tap = _Tap()
        with self._cond:
            self._taps.append(tap)
        gen = self._read(tap, chunk, timeout_s)
        weakref.finalize(gen, self._close_tap, tap)  # a generator that is never iterated still releases its tap
        return gen

    def _read(self, tap: _Tap, chunk: int, timeout_s: float):
        chunk_s = chunk / RATE
        try:
            while True:
                with self._cond:
                    if tap.samples < chunk:  # connected: allow for jitter; no phone: tick at the pace of a real mic
                        self._cond.wait_for(lambda: tap.samples >= chunk, timeout_s if self._ws is not None else chunk_s)
                    out = tap.take(chunk)
                yield np.zeros(chunk, dtype=np.int16) if out is None else out
        finally:
            self._close_tap(tap)

    def _close_tap(self, tap: _Tap):
        with self._cond:
            if tap in self._taps:
                self._taps.remove(tap)

    # ---- us -> phone -----------------------------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ws is not None

    def send(self, pcm: bytes) -> bool:
        """Send PCM16 mono 16 kHz to the phone in small frames, paced a little ahead of real time. Blocks until
        everything is sent; False when no phone is connected or it drops part-way."""
        pcm = bytes(pcm[:len(pcm) - (len(pcm) % 2)])  # the phone carries an odd byte over: it would shift the next utterance
        with self._tx_lock:
            loop, ws = self._loop, self._ws
            if loop is None or ws is None:
                return False
            if not pcm:
                return True
            try:
                future = asyncio.run_coroutine_threadsafe(self._send_paced(ws, pcm), loop)
            except RuntimeError:
                return False
            try:
                return bool(future.result(timeout=len(pcm) / BYTES_PER_S + 10.0))
            except Exception:
                future.cancel()
                return False

    async def _send_paced(self, ws, pcm: bytes) -> bool:
        from websockets.exceptions import ConnectionClosed
        started, sent = time.monotonic(), 0
        try:
            for offset in range(0, len(pcm), TX_FRAME_BYTES):
                frame = pcm[offset:offset + TX_FRAME_BYTES]
                await ws.send(frame)
                sent += len(frame)
                self._tx_bytes += len(frame)
                ahead = sent / BYTES_PER_S - TX_LEAD_S - (time.monotonic() - started)
                if ahead > 0:
                    await asyncio.sleep(ahead)
        except ConnectionClosed:
            return False
        return True

    def play_pcm(self, pcm: bytes, tail_s: float = 0.15) -> bool:
        """send() and then wait out the rest of the audio, so the caller does not listen over Annie's own voice."""
        started = time.monotonic()
        if not self.send(pcm):
            return False
        remaining = len(pcm) / BYTES_PER_S + tail_s - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        return True

    def play(self, data: bytes, suffix: str = ".mp3", timeout_s: float = 60.0) -> bool:
        """Encoded audio (ElevenLabs mp3, `say` aiff, wav) -> the phone's speaker. Blocks for the audio's
        duration. False when no phone is connected (checked before decoding) or decoding fails."""
        if not data or not self.connected:
            return False
        pcm = self.decoder(data, suffix, timeout_s)
        if not pcm:
            return False
        return self.play_pcm(pcm)

    # ---- status ----------------------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._cond:
            return {"listening": self._listening, "connected": self._ws is not None, "peer": self._peer,
                    "rx_packets": self._rx_packets, "tx_bytes": self._tx_bytes,
                    "port": self._bound_port if self._listening and self._bound_port else self.port,
                    "path": self.path, "error": self._error}
