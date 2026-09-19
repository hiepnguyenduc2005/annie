"""Legacy status-only v1 envelope. Rich v0.1 payloads use contract/schemas.json.

This type is retained for simple status producers; it is no longer the complete
robot-to-app protocol. See contract/README.md for approved richer channels.
"""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class RobotSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    signal_id: UUID = Field(default_factory=uuid4)
    status: Literal["ready", "busy", "completed", "failed", "unavailable"]
