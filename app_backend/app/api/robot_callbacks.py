from fastapi import APIRouter, Depends
from .dependencies import services, robot_auth, IdempotencyKey
from ..schemas.reminders import NewNote, Note
from ..schemas.messages import MessageReply, MessageEntry
from ..schemas.notifications import NewNotification, Notification

router = APIRouter(prefix='/api', tags=['robot callbacks'], dependencies=[Depends(robot_auth)])


@router.post('/notes', response_model=Note, status_code=201)
async def note(body: NewNote, key: IdempotencyKey, svc=Depends(services)):
    return await svc.reminders.add_note(body, key)


@router.post('/messages/replies', response_model=MessageEntry, status_code=201)
async def reply(body: MessageReply, key: IdempotencyKey, svc=Depends(services)):
    return await svc.messages.reply(body, key)


@router.post('/notifications', response_model=Notification, status_code=201)
async def notification(body: NewNotification, key: IdempotencyKey, svc=Depends(services)):
    return await svc.notifications.create(body, key)
