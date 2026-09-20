"""Phone transport: half-duplex PCM with session control and playback receipts."""

import asyncio
import base64
import io
import logging
import wave
from collections import deque
from contextlib import suppress
from typing import Literal
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import Field, ValidationError

from ..schemas import StrictModel
from ..sessions.manager import SessionError
from .deepgram import SpeechError
from .deepgram_live import Transcript
from .dedicated_server import DeliveryError
from .qwen import QwenError


logger = logging.getLogger(__name__)


class AudioControl(StrictModel):
    type: Literal["start", "stop", "playback_finished"]
    session_id: str | None = Field(None, max_length=128)
    turn_id: str | None = Field(None, max_length=128)


def pcm_from_wav(encoded: str) -> bytes:
    try:
        with wave.open(
            io.BytesIO(base64.b64decode(encoded, validate=True)), "rb"
        ) as source:
            if (
                source.getframerate(),
                source.getnchannels(),
                source.getsampwidth(),
                source.getcomptype(),
            ) != (16000, 1, 2, "NONE"):
                raise ValueError
            if not 0 < source.getnframes() <= 16000 * 90:
                raise ValueError
            frames = source.readframes(source.getnframes())
            if len(frames) != source.getnframes() * 2:
                raise ValueError
            return frames
    except (ValueError, wave.Error, EOFError):
        raise SpeechError("Reply is not valid 16 kHz PCM audio") from None


class AudioHub:
    """One resident phone; pending remote tasks contain only existing session IDs."""

    def __init__(self):
        self.connection = None
        self.pending: deque[str] = deque()
        self.available = asyncio.Event()

    def offer(self, session_id: str) -> None:
        if session_id in self.pending or (
            self.connection and self.connection.session_id == session_id
        ):
            return
        if len(self.pending) >= 16:
            raise SessionError(
                503, "Phone task queue is full; retry with the same request_id"
            )
        self.pending.append(session_id)
        self.available.set()

    def claim(self, session_id: str) -> None:
        """Remove a task only after the phone explicitly accepts its session."""
        with suppress(ValueError):
            self.pending.remove(session_id)
        if not self.pending:
            self.available.clear()

    def take(self) -> str | None:
        result = self.pending.popleft() if self.pending else None
        if not self.pending:
            self.available.clear()
        return result


class AudioConversation:
    def __init__(self, websocket: WebSocket, agent, live, hub: AudioHub):
        self.ws, self.agent, self.live, self.hub = websocket, agent, live, hub
        self.session_id: str | None = None
        self.remote = False
        self.phase = "stopped"
        self.worker: asyncio.Task | None = None
        self.packets: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.played = asyncio.Event()
        self.turn_id: str | None = None
        self.send_lock = asyncio.Lock()
        self.offered_session_id: str | None = None

    async def event(self, type: str, **fields):
        async with self.send_lock:
            await asyncio.wait_for(self.ws.send_json({"type": type, **fields}), 10)

    async def state(self, phase: str):
        self.phase = phase
        await self.event("state", state=phase)

    def clear_packets(self):
        while not self.packets.empty():
            self.packets.get_nowait()

    async def cancel_worker(self):
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await self.worker
            self.worker = None
        self.clear_packets()
        self.turn_id = None

    async def announce_pending(self):
        """Wake a connected, idle phone without consuming unacknowledged work."""
        while True:
            await self.hub.available.wait()
            if self.hub.pending and (self.worker is None or self.worker.done()):
                candidate = self.hub.pending[0]
                try:
                    async with self.agent.sessions.locked(candidate) as session:
                        active = session.status == "active"
                except SessionError:
                    active = False
                if not active:
                    self.hub.claim(candidate)
                elif candidate != self.offered_session_id:
                    self.offered_session_id = candidate
                    await self.event("conversation_requested", session_id=candidate)
            await asyncio.sleep(.25)

    async def run(self):
        announcements = None
        try:
            await self.event("ready", protocol=1, sample_rate=16000)
            announcements = asyncio.create_task(self.announce_pending())
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    packet = message["bytes"]
                    if not packet or len(packet) > 640 or len(packet) % 2:
                        await self.ws.close(1009, "Expected 20 ms PCM packets")
                        break
                    if self.phase == "listening":
                        if self.packets.full():
                            await self.event(
                                "error",
                                message="Audio connection is too slow; restart audio",
                            )
                            await self.cancel_worker()
                            await self.state("stopped")
                        else:
                            self.packets.put_nowait(packet)
                    continue
                text = message.get("text", "")
                try:
                    if len(text) > 4096:
                        raise ValueError
                    control = AudioControl.model_validate_json(text)
                except (ValidationError, ValueError):
                    await self.ws.close(1008, "Invalid audio control")
                    break
                if control.type == "playback_finished":
                    if control.turn_id == self.turn_id and self.phase == "speaking":
                        self.played.set()
                elif control.type == "start":
                    if self.worker and not self.worker.done():
                        continue
                    if control.session_id:
                        self.session_id = control.session_id
                        self.hub.claim(control.session_id)
                    self.worker = asyncio.create_task(self.converse())
                elif control.type == "stop":
                    await self.cancel_worker()
                    if self.session_id:
                        try:
                            await self.agent.end(self.session_id)
                        except (SessionError, DeliveryError):
                            await self.event(
                                "error",
                                message="Final result is pending; the server will retry cleanup",
                            )
                    self.session_id = None
                    await self.event("session", session_id=None)
                    await self.state("stopped")
                    # The maintenance worker delivers the durable outbox.
                    # Never block receiving disconnect/start on callback HTTP.
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if announcements is not None:
                announcements.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await announcements
            await self.cancel_worker()

    async def bind(self, session_id: str | None) -> bool:
        """Return whether a remote task needs its opening question."""
        if session_id:
            try:
                async with self.agent.sessions.locked(session_id) as session:
                    if session.status != "active":
                        session_id = None
                    else:
                        self.remote = session.request_id is not None
                        opening = self.remote and not session.started
            except SessionError:
                session_id = None
        if session_id is None:
            session = await self.agent.sessions.create()
            session_id = session.session_id
            self.remote, opening = False, False
        self.session_id = session_id
        await self.event("session", session_id=session_id)
        return opening

    async def listen(self) -> str | None:
        self.clear_packets()
        transcript = Transcript()
        listening = asyncio.create_task(
            self.live.listen(self.packets, lambda: self.state("listening"), transcript)
        )
        queued = (
            asyncio.create_task(self.hub.available.wait()) if not self.remote else None
        )
        tasks = [listening] + ([queued] if queued else [])
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=self.agent.settings.session_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if listening in done:
                return await listening
            if transcript.heard_speech:
                return await asyncio.wait_for(listening, 60)
            return None
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, SpeechError):
                    await task

    async def converse(self):
        try:
            opening = await self.bind(self.session_id)
            while True:
                if not self.remote and self.hub.pending:
                    candidate = self.hub.take()
                    try:
                        async with self.agent.sessions.locked(
                            candidate
                        ) as task_session:
                            valid = task_session.status == "active"
                    except SessionError:
                        valid = False
                    if valid:
                        if self.session_id:
                            await self.agent.end(self.session_id)
                        opening = await self.bind(candidate)
                if opening:
                    text = None
                else:
                    text = await self.listen()
                    if text is None:
                        continue
                await self.state("thinking")
                result = await self.agent.generate(
                    self.session_id,
                    [{"type": "text", "text": text}] if text else [],
                    start=opening,
                    sample_rate=16000,
                )
                opening = False
                pcm = pcm_from_wav(result.audio)
                self.turn_id = str(uuid4())
                self.played.clear()
                await self.state("speaking")
                await self.event(
                    "audio_start",
                    turn_id=self.turn_id,
                    sample_rate=16000,
                    bytes=len(pcm),
                )
                for offset in range(0, len(pcm), 16000):
                    async with self.send_lock:
                        await asyncio.wait_for(
                            self.ws.send_bytes(pcm[offset : offset + 16000]), 10
                        )
                await self.event("audio_end", turn_id=self.turn_id, done=result.done)
                await asyncio.wait_for(
                    self.played.wait(),
                    len(pcm) / 32000 + self.agent.settings.audio_playback_grace_seconds,
                )
                await self.event("playback_acknowledged", turn_id=self.turn_id)
                self.turn_id = None
                if result.done:
                    await self.event("session_ended", session_id=self.session_id)
                    if self.remote:
                        self.session_id = None
                        await self.state("stopped")
                        return
                    opening = await self.bind(None)
        except asyncio.TimeoutError:
            logger.warning("Audio conversation timed out (phase=%s)", self.phase)
            if self.phase == "speaking":
                await self.event(
                    "error",
                    message="Playback was not confirmed; restart audio to continue",
                )
            else:
                if self.session_id:
                    await self.agent.end(self.session_id, "timeout")
                self.session_id = None
                await self.event("session", session_id=None)
            await self.state("stopped")
        except (SpeechError, QwenError, SessionError, DeliveryError) as exc:
            reason = exc.message if isinstance(exc, SessionError) else str(exc)
            logger.warning("Audio conversation failed (phase=%s, category=%s): %s",
                           self.phase, type(exc).__name__, reason)
            await self.event(
                "error",
                message="Conversation service is unavailable; restart audio to retry",
            )
            await self.state("stopped")
        except WebSocketDisconnect:
            pass
        except (RuntimeError, OSError) as exc:
            logger.warning("Audio transport failed (phase=%s, category=%s)",
                           self.phase, type(exc).__name__)
        finally:
            self.clear_packets()
