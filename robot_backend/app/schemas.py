from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]
TaskStatus = Literal["active", "completed", "cancelled", "failed", "inconclusive"]
FinalStatus = Literal["completed", "cancelled", "failed", "inconclusive"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class RemoteRequest(StrictModel):
    request_id: Identifier
    type: Identifier
    request: str = Field(min_length=1, max_length=2000)


class Acknowledgment(StrictModel):
    session_id: str
    accepted: bool = True
    done: bool = False


class GenerateResponse(StrictModel):
    session_id: str
    audio: str
    done: bool


class EndRequest(StrictModel):
    reason: Literal["ended", "cancelled"] = "ended"


class EndResponse(StrictModel):
    session_id: str
    done: bool = True


class FinalEvent(StrictModel):
    session_id: str
    request_id: str | None
    type: str
    status: FinalStatus
    summary: str = Field(min_length=1, max_length=240)


class TurnAnalysis(StrictModel):
    conversation_done: bool
    task_status: TaskStatus
    # Replaces older context, while the recent turns remain available separately.
    rolling_memory: str = Field(max_length=2000)
    user_memory: str = Field(max_length=1000)
    assistant_memory: str = Field(max_length=1000)
    goal_supported: bool


class SummaryAnalysis(StrictModel):
    status: FinalStatus
    summary: str = Field(min_length=1, max_length=240)

    @field_validator("summary")
    @classmethod
    def no_dialogue(cls, value: str) -> str:
        if any(char in value for char in ("\n", "\r", '"', "“", "”", "{", "}")):
            raise ValueError("Summary must be a short unquoted outcome")
        return value
