"""Bounded frame input and image-free inference output."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app_backend.app.models import Perception

MAX_JPEG_BYTES = 1_000_000
MAX_BASE64_CHARS = 4 * ((MAX_JPEG_BYTES + 2) // 3)
MAX_BODY_BYTES = MAX_BASE64_CHARS + 4096


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class CapturePose(StrictModel):
    x: float
    y: float
    yaw: float
    map_id: str = Field(min_length=1, max_length=100)


class FrameRequest(StrictModel):
    frame_id: UUID
    ts: int = Field(ge=0)
    pose: CapturePose
    source: Literal['simulation_render', 'hardware']
    jpeg_b64: str = Field(min_length=4, max_length=MAX_BASE64_CHARS, repr=False)

    @field_validator('frame_id', mode='before')
    @classmethod
    def canonical_uuid(cls, value):
        if not isinstance(value, str):
            raise ValueError('frame_id must be a canonical UUID string')
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError('frame_id must be a canonical UUID string')
        return parsed


class Observation(StrictModel):
    person: bool
    posture: Literal['standing', 'sitting', 'lying', 'unknown']
    location: Literal['bed', 'floor', 'chair', 'unknown']
    confidence: float = Field(ge=0, le=1)
    caption: str = Field(max_length=2000)


class ProviderUsage(StrictModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ProviderMetadata(StrictModel):
    mode: Literal['local', 'cloud']
    model: str
    usage: ProviderUsage | None = None


class InferenceResult(StrictModel):
    perception: Perception
    provider: ProviderMetadata
    source: Literal['simulation_render', 'hardware']
    latency_ms: float = Field(ge=0)
