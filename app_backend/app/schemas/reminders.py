from typing import Annotated, Literal
from pydantic import Field, AwareDatetime
from .common import ID, Text, Schema, Record

DailyTime = Annotated[str, Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')]


class NewReminder(Schema):
    app_user_id: ID
    daily_time: DailyTime
    description: Text


class Reminder(Record):
    dog_user_id: ID
    daily_time: DailyTime
    description: Text
    enabled: bool = True


class NewNote(Schema):
    request_id: ID
    occurrence_id: ID
    reminder_id: ID
    timestamp: AwareDatetime
    description: Text
    outcome: Literal['completed', 'not_completed', 'unknown']


class Note(NewNote, Record):
    dog_user_id: ID
    source: Literal['robot', 'seed']
    day: str


class ReminderView(Reminder):
    done: bool
    latest_note: Note | None = None


from .common import Page


class ReminderPage(Page[ReminderView]):
    day: str
