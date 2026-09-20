import asyncio
import base64
import logging

from .. import prompts
from ..config import Settings
from ..schemas import (
    Acknowledgment,
    FinalEvent,
    GenerateResponse,
    RemoteRequest,
    SummaryAnalysis,
    TurnAnalysis,
)
from ..sessions.manager import SessionError, SessionManager
from ..sessions.models import Session, utc_now
from .dedicated_server import DeliveryError, FinalEventSink
from .qwen import ModelClient, QwenError
from .deepgram import SpeechClient, SpeechError

logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionManager,
        model: ModelClient,
        sink: FinalEventSink,
        speech: SpeechClient,
    ):
        self.settings, self.sessions, self.model, self.sink = (
            settings,
            sessions,
            model,
            sink,
        )
        self.speech = speech

    async def request(self, request: RemoteRequest) -> Acknowledgment:
        session = await self.sessions.create(request)
        return Acknowledgment(
            session_id=session.session_id, done=session.status != "active"
        )

    async def generate(
        self,
        session_id: str | None,
        content: list[dict],
        start: bool = False,
        *,
        sample_rate: int = 24000,
    ) -> GenerateResponse:
        if not content and not (start and session_id):
            raise SessionError(
                400,
                "Supply text or audio; start=true requires an existing remote session",
            )
        if start and (not session_id or content):
            raise SessionError(
                400, "start=true requires an existing remote session and no input"
            )
        created = session_id is None
        if created:
            session_id = (await self.sessions.create()).session_id
        async with self.sessions.locked(session_id) as session:
            if session.status != "active":
                raise SessionError(410, "Session has ended")
            if self._expired(session):
                await self._finalize(session, "timeout")
                raise SessionError(410, "Session has expired")
            if start and (session.started or not session.goal):
                raise SessionError(
                    409, "start=true is only valid once for an unstarted remote task"
                )
            current = content or [{"type": "text", "text": prompts.START_PROMPT}]
            try:
                # Transcribe once; both Qwen calls receive text, never past audio.
                prepared = []
                for part in current:
                    if part["type"] == "audio_url":
                        header, encoded = part["audio_url"]["url"].split(",", 1)
                        mime = header[5:].split(";", 1)[0]
                        transcript = await self.speech.transcribe(
                            base64.b64decode(encoded, validate=True), mime
                        )
                        prepared.append({"type": "text", "text": transcript})
                    else:
                        prepared.append(part)
                current = prepared
                if content:
                    # Capture received evidence before any cancellable model/TTS call.
                    text = " ".join(part["text"] for part in current if part["type"] == "text")
                    session.pending_resident_text = (session.pending_resident_text + "\n" + text).strip()[-4000:]
                    session.user_turns += 1
                    session.updated_at = utc_now()
                spoken_text = await self.model.reply(
                    prompts.conversation_messages(session, current)
                )
                audio = await self.speech.synthesize(
                    spoken_text, sample_rate=sample_rate
                )
            except (QwenError, SpeechError):
                if created:
                    await self.sessions.discard(session.session_id)
                raise
            analysis_succeeded = True
            try:
                analysis = await self.model.structured(
                    prompts.analysis_messages(session, current, spoken_text),
                    TurnAnalysis,
                )
            except QwenError as exc:
                analysis_succeeded = False
                logger.warning(
                    "Turn analysis unavailable (%s); returning audio with conservative state", exc
                )
                text = " ".join(
                    part["text"] for part in current if part["type"] == "text"
                )
                analysis = TurnAnalysis(
                    conversation_done=False,
                    task_status=session.task_status,
                    goal_supported=session.goal_supported,
                    rolling_memory=session.rolling_memory,
                    user_memory=text[:1000] or prompts.NO_MEMORY,
                    assistant_memory=spoken_text[:1000],
                )
            session.started = True
            if analysis_succeeded:
                session.pending_resident_text = ""
            session.rolling_memory = analysis.rolling_memory
            session.history.extend(
                [
                    {"role": "user", "content": analysis.user_memory},
                    {"role": "assistant", "content": analysis.assistant_memory},
                ]
            )
            session.history = session.history[-2 * self.settings.history_turns :]
            session.goal_supported = analysis.goal_supported and session.user_turns > 0
            session.task_status = analysis.task_status
            if (
                session.goal
                and not session.goal_supported
                and session.task_status == "completed"
            ):
                session.task_status = "active"
            if start:
                session.goal_supported = False
                session.task_status = "active"
            session.updated_at = utc_now()
            if analysis.conversation_done and not start:
                try:
                    await self._finalize(session, "natural")
                except DeliveryError:
                    # Retain only the final event for maintenance to enqueue again.
                    logger.error("Final-result queue unavailable; retry pending")
            return GenerateResponse(
                session_id=session.session_id,
                audio=audio,
                done=session.status != "active",
            )

    def _expired(self, session: Session) -> bool:
        return (
            utc_now() - session.updated_at
        ).total_seconds() >= self.settings.session_timeout_seconds

    async def end(self, session_id: str, reason: str = "ended") -> None:
        async with self.sessions.locked(session_id) as session:
            await self._finalize(session, reason)

    async def _finalize(self, session: Session, reason: str) -> None:
        if session.final_event is None:
            try:
                if reason == "timeout":
                    # Do not let a slow/offline model prevent timely memory expiry.
                    summary = SummaryAnalysis(
                        status="inconclusive",
                        summary="The conversation timed out; its final outcome could not be confirmed.",
                    )
                else:
                    summary = await asyncio.wait_for(
                        self.model.structured(
                            prompts.summary_messages(session, reason), SummaryAnalysis
                        ),
                        timeout=min(30, self.settings.qwen_request_timeout),
                    )
            except (QwenError, asyncio.TimeoutError):
                summary = SummaryAnalysis(
                    status="inconclusive",
                    summary="The conversation ended; its outcome could not be determined.",
                )
            if session.request_id and session.user_turns == 0:
                summary = SummaryAnalysis(
                    status="inconclusive",
                    summary="The requested check-in or task outcome could not be determined.",
                )
            elif session.request_id and (
                not session.goal_supported or session.pending_resident_text
            ) and summary.status == "completed":
                summary = SummaryAnalysis(
                    status="inconclusive",
                    summary="The resident replied, but completion of the requested task was not confirmed.",
                )
            logger.info("Conversation finalized (reason=%s, received_turns=%s, unassessed_reply=%s, goal_supported=%s, outcome=%s)",
                        reason, session.user_turns, bool(session.pending_resident_text), session.goal_supported, summary.status)
            if reason == "cancelled":
                summary = SummaryAnalysis(
                    status="cancelled",
                    summary="The conversation or task was cancelled.",
                )
            session.final_event = FinalEvent(
                session_id=session.session_id,
                request_id=session.request_id,
                type=session.type,
                status=summary.status,
                summary=summary.summary,
            )
            session.task_status = summary.status
            session.status = "expired" if reason == "timeout" else "ended"
            session.updated_at = utc_now()
            session.erase_details()
        if not session.enqueued:
            await self.sink.enqueue(session.final_event)
            session.enqueued = True

    async def maintain(self) -> None:
        for session_id in await self.sessions.maintenance_ids():
            try:
                async with self.sessions.locked(session_id) as session:
                    if session.status != "active" or self._expired(session):
                        await self._finalize(session, "timeout")
            except (SessionError, DeliveryError):
                continue
