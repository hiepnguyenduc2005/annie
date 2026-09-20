from datetime import date
from fastapi import APIRouter, Depends, Query
from ..schemas.common import Page
from .dependencies import services, UserID, Limit, Cursor, IdempotencyKey
from ..schemas.reminders import NewReminder, Reminder, ReminderPage, Note

router = APIRouter(prefix='/api', tags=['reminders'])


@router.get('/reminders', response_model=ReminderPage)
async def reminders(app_user_id: UserID, day: date | None = None, limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.reminders.list(app_user_id, day, limit, cursor)


@router.post('/reminders', response_model=Reminder, status_code=201)
async def new_reminder(body: NewReminder, key: IdempotencyKey, svc=Depends(services)):
    return await svc.reminders.create(body, key)


@router.get('/notes', response_model=Page[Note])
async def notes(app_user_id: UserID, reminder_id: int | None = Query(default=None, ge=1), limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.reminders.notes(app_user_id, reminder_id, limit, cursor)
