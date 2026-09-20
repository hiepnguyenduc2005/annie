from pydantic import AwareDatetime, StrictBool
from typing import Literal
from .common import ID, Text, Schema, Record


class NewNotification(Schema):
    dog_user_id: ID
    timestamp: AwareDatetime
    description: Text
    is_emergency: StrictBool


class Notification(NewNotification, Record):
    source: Literal['robot'] = 'robot'


class HistoryItem(Record):
    kind: Literal['note', 'notification']
    dog_user_id: ID
    description: Text
    is_emergency: bool
    source: Literal['robot', 'seed']
    reminder_id: ID | None = None
    outcome: Literal['completed', 'not_completed', 'unknown'] | None = None
