import secrets
from datetime import date, datetime, timezone, timedelta
from typing import Annotated
from fastapi import APIRouter, Header, HTTPException, Request, Depends
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

ID = Annotated[int, Field(strict=True, gt=0, le=9007199254740991)]

class MessageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: ID
    conversation_id: ID
    message_id: ID
    app_user_id: ID
    dog_user_id: ID
    day: date
    text: str = Field(min_length=1, max_length=4000)

class ReminderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: ID
    occurrence_id: ID
    dog_user_id: ID
    reminder_id: ID
    day: date
    daily_time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    scheduled_at: AwareDatetime
    description: str = Field(min_length=1, max_length=4000)

async def authorize(request: Request, x_internal_secret: str = Header(default='')):
    expected = request.app.state.settings.internal_secret.get_secret_value()
    if not expected or not secrets.compare_digest(expected, x_internal_secret):
        raise HTTPException(401, 'Invalid internal secret')

router = APIRouter(dependencies=[Depends(authorize)])

@router.post('/api/message-requests', status_code=202)
async def message(body: MessageRequest, request: Request):
    return await request.app.state.companion.accept(body, 'message', request.app.state.agent, request.app.state.audio_hub)

@router.post('/api/reminder-requests', status_code=202)
async def reminder(body: ReminderRequest, request: Request):
    if datetime.now(timezone.utc) - body.scheduled_at > timedelta(minutes=15):
        raise HTTPException(409, 'Reminder occurrence has expired')
    if body.occurrence_id != body.request_id:
        raise HTTPException(422, 'Occurrence must match request')
    return await request.app.state.companion.accept(body, 'reminder', request.app.state.agent, request.app.state.audio_hub)
