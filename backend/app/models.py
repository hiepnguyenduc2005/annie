"""Strict, image-free application contracts. Poses locate the observing robot."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

Timestamp = Annotated[int, Field(strict=True, ge=0)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

class Versioned(StrictModel):
    schema_version: Literal['0.1'] = '0.1'

class Point(StrictModel):
    x: float
    y: float

class Pose(Point):
    yaw: float = 0
    map_id: str = Field(default='demo-home', min_length=1, max_length=100)

class DogStatus(Versioned):
    ts: Timestamp
    state: Literal['idle', 'patrolling', 'paused', 'estop', 'lowbatt']
    battery_pct: float = Field(ge=0, le=100)
    waypoint: str | None = Field(default=None, max_length=100)
    pose: Pose

class Waypoint(Point):
    id: str = Field(min_length=1, max_length=100)

class Room(Point):
    id: str
    label: str
    width: float = Field(gt=0)
    height: float = Field(gt=0)

class DogMap(Versioned):
    ts: Timestamp
    map_id: str = Field(min_length=1, max_length=100)
    png_b64: str | None = Field(default=None, max_length=500000)
    origin: Point
    resolution_m: float = Field(gt=0)
    waypoints: list[Waypoint] = Field(max_length=100)
    rooms: list[Room] = Field(default_factory=list, max_length=100)

class Perception(Versioned):
    ts: Timestamp
    frame_id: UUID
    person: bool
    posture: Literal['standing', 'sitting', 'lying', 'unknown']
    location: Literal['bed', 'floor', 'chair', 'unknown']
    confidence: Confidence
    caption: str = Field(max_length=2000)
    pose: Pose

class VoiceHeard(Versioned):
    ts: Timestamp
    text: Text
    confidence: Confidence
    speaker: Literal['resident'] = 'resident'
    event_id: UUID | None = None

class Evidence(StrictModel):
    frame_id: UUID
    pose: Pose
    crop_url: str | None = None

class Event(Versioned):
    ts: Timestamp
    event_id: UUID
    kind: Literal['checkin_ok', 'checkin_no_reply', 'fall_suspected', 'fall_confirmed', 'reminder_due']
    evidence: Evidence
    severity: Literal['info', 'warn', 'critical']
    acknowledged: bool = False
    acknowledged_by: str | None = None
    acknowledged_at: Timestamp | None = None

class Say(StrictModel):
    text: Text

class Command(StrictModel):
    cmd: Literal['stop', 'resume', 'look', 'goto']
    waypoint: str | None = Field(default=None, max_length=100)

class Ack(StrictModel):
    by: Literal['family']

class Scenario(StrictModel):
    scenario: Literal['safe_bed', 'fall', 'help', 'okay', 'timeout']

class Ingest(StrictModel):
    channel: Literal['dog.status', 'dog.map', 'brain.perception', 'voice.heard']
    data: dict

CHANNEL_MODELS = {'dog.status': DogStatus, 'dog.map': DogMap, 'brain.perception': Perception, 'voice.heard': VoiceHeard}
