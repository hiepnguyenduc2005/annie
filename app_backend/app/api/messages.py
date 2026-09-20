from datetime import date
from fastapi import APIRouter, Depends, Path
from .dependencies import services, UserID, Limit, Cursor, IdempotencyKey
from ..schemas.messages import NewMessage, ConversationPage, DispatchReceipt, DispatchStatus

router = APIRouter(prefix='/api', tags=['messages'])


@router.get('/messages', response_model=ConversationPage)
async def messages(app_user_id: UserID, day: date | None = None, limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.messages.list(app_user_id, day, limit, cursor)


@router.post('/messages', status_code=202, response_model=DispatchReceipt)
async def new_message(body: NewMessage, key: IdempotencyKey, svc=Depends(services)):
    return await svc.messages.create(body, key)


@router.get('/requests/{request_id}', response_model=DispatchStatus)
async def request_status(request_id: int, app_user_id: UserID, svc=Depends(services)):
    return await svc.robot.get(request_id, app_user_id)
