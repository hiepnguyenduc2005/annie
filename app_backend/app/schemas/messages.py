from typing import Literal
from pydantic import AwareDatetime, Field
from .common import ID, Text, Schema


class NewMessage(Schema):
    app_user_id: ID
    text: Text


class MessageReply(Schema):
    request_id: ID
    reply_to: ID
    role: Literal['robot', 'resident']
    text: Text
    timestamp: AwareDatetime
    final: bool = True


class MessageEntry(Schema):
    id: ID
    role: Literal['app_user', 'robot', 'resident']
    text: Text
    timestamp: AwareDatetime
    request_id: ID
    reply_to: ID | None = None
    status: Literal['queued', 'accepted', 'completed', 'failed'] | None = None


class Conversation(Schema):
    id: ID
    app_user_id: ID
    dog_user_id: ID
    day: str
    messages: list[MessageEntry] = Field(default_factory=list)


class ConversationPage(Schema):
    id: ID | None
    app_user_id: ID
    dog_user_id: ID
    day: str
    messages: list[MessageEntry]
    next_cursor: str | None = None


class DispatchReceipt(Schema):
    conversation_id: ID
    message_id: ID
    request_id: ID
    day: str
    status: Literal['queued']


class DispatchStatus(Schema):
    id: ID
    kind: Literal['message', 'reminder']
    status: Literal['queued', 'sending', 'accepted', 'completed', 'failed', 'missed', 'seed']
    timestamp: AwareDatetime
    attempts: int | None = None
    error: str | None = None
    day: str
