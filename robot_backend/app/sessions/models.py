import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from ..schemas import FinalEvent, TaskStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    request_id: str | None = None
    type: str = "conversation"
    goal: str | None = None
    request_fingerprint: str | None = None
    history: list[dict] = field(default_factory=list)
    rolling_memory: str = ""
    pending_resident_text: str = ""
    task_status: TaskStatus = "active"
    goal_supported: bool = False
    user_turns: int = 0
    started: bool = False
    status: str = "active"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    final_event: FinalEvent | None = None
    enqueued: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def erase_details(self) -> None:
        self.pending_resident_text = ""
        self.history.clear()
        self.rolling_memory = ""
        self.goal = None
