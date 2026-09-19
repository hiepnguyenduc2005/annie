"""Minimal robot-to-app status contract; no arbitrary content is allowed."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class RobotSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    signal_id: UUID = Field(default_factory=uuid4)
    status: Literal["ready", "busy", "completed", "failed", "unavailable"]
