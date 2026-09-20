"""Request bodies for the household schema routes."""
from typing import Literal

from pydantic import Field

from .models import StrictModel, Text, Timestamp

Name = str


class NewDogUser(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    # Lets a caller keep an id it already assigned (the phone registers
    # offline first), rather than forcing a second identifier.
    id: str | None = Field(default=None, min_length=1, max_length=64)


class NewAppUser(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    dog_user_id: str = Field(min_length=1, max_length=64)


class NewSchemaMessage(StrictModel):
    dog_user_id: str = Field(min_length=1, max_length=64)
    app_user_id: int | None = None
    text: Text


class NewSchemaReminder(StrictModel):
    dog_user_id: str = Field(min_length=1, max_length=64)
    hour: int = Field(ge=0, le=23)
    item: Text


# ---- inbound from robot_backend ------------------------------------------

class MessageReplyIn(StrictModel):
    """Yellow: summary/analysis of the dog's action, or the resident's audio
    response, appended to the message's texts."""
    message_id: str = Field(min_length=1, max_length=64)
    text: Text
    source: Literal['robot', 'resident'] = 'robot'


class ReminderHistoryIn(StrictModel):
    """Red: what actually happened for a reminder."""
    reminder_id: str = Field(min_length=1, max_length=64)
    description: Text
    timedate: Timestamp | None = None


class EmergencyIn(StrictModel):
    """Cyan: emergency text received from robot_backend."""
    dog_user_id: str = Field(min_length=1, max_length=64)
    description: Text
    timestamp: Timestamp | None = None
