import hashlib
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from ..config import Settings
from ..schemas import RemoteRequest
from .models import Session, utc_now


class SessionError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


class SessionManager(Protocol):
    """Replaceable unit-of-work boundary; locked() commits mutations on exit.

    A shared store must implement distributed locking and atomic create/dedup.
    """

    async def create(self, request: RemoteRequest | None = None) -> Session: ...
    def locked(self, session_id: str) -> AbstractAsyncContextManager[Session]: ...
    async def maintenance_ids(self) -> list[str]: ...
    async def discard(self, session_id: str) -> None: ...
    async def clear(self) -> None: ...


class InMemorySessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sessions: dict[str, Session] = {}
        self._requests: dict[str, str] = {}

    async def create(self, request: RemoteRequest | None = None) -> Session:
        # No awaits: lookup + insertion is atomic within this single event loop.
        fingerprint = None
        if request:
            fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
            existing_id = self._requests.get(request.request_id)
            if existing_id:
                existing = self._sessions[existing_id]
                if existing.request_fingerprint != fingerprint:
                    raise SessionError(
                        409, "request_id already belongs to a different request"
                    )
                return existing
        self._prune()
        if len(self._sessions) >= self.settings.max_sessions:
            raise SessionError(503, "Session capacity reached; retry later")
        session = Session(
            request_id=request.request_id if request else None,
            type=request.type if request else "conversation",
            goal=request.request if request else None,
            request_fingerprint=fingerprint,
        )
        self._sessions[session.session_id] = session
        if request:
            self._requests[request.request_id] = session.session_id
        return session

    @asynccontextmanager
    async def locked(self, session_id: str):
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(404, "Unknown session_id")
        async with session.lock:
            yield session

    async def discard(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.erase_details()

    async def clear(self) -> None:
        for session in self._sessions.values():
            session.erase_details()
        self._sessions.clear()
        self._requests.clear()

    def _prune(self) -> None:
        now = utc_now()
        for session_id, session in list(self._sessions.items()):
            age = (now - session.updated_at).total_seconds()
            if (
                session.status != "active"
                and session.enqueued
                and not session.lock.locked()
                and age > self.settings.session_retention_seconds
            ):
                del self._sessions[session_id]
                if session.request_id:
                    self._requests.pop(session.request_id, None)

    async def maintenance_ids(self) -> list[str]:
        self._prune()
        now = utc_now()
        return [
            s.session_id
            for s in self._sessions.values()
            if not s.lock.locked()
            and (
                (s.status != "active" and not s.enqueued)
                or (
                    s.status == "active"
                    and (now - s.updated_at).total_seconds()
                    >= self.settings.session_timeout_seconds
                )
            )
        ]
