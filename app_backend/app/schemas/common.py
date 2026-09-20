from datetime import datetime, timezone
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

MAX_ID = 9007199254740991
ID = Annotated[int, Field(strict=True, ge=1, le=MAX_ID)]
Text = Annotated[str, Field(min_length=1, max_length=4000)]
Name = Annotated[str, Field(min_length=1, max_length=100)]


class Schema(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


def utcnow():
    return datetime.now(timezone.utc)


def iso(value: datetime):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class Record(Schema):
    id: ID
    timestamp: AwareDatetime


from typing import Generic, TypeVar
T = TypeVar('T')


class Page(Schema, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
