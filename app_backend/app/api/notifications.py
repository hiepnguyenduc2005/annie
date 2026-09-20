from fastapi import APIRouter, Depends
from ..schemas.common import Page
from ..schemas.notifications import Notification, HistoryItem
from .dependencies import services, UserID, Limit, Cursor

router = APIRouter(prefix='/api', tags=['history'])


@router.get('/notifications', response_model=Page[Notification])
async def notifications(app_user_id: UserID, is_emergency: bool | None = None, limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.notifications.list(app_user_id, is_emergency, limit, cursor)


@router.get('/history', response_model=Page[HistoryItem])
async def history(app_user_id: UserID, limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.notifications.history(app_user_id, limit, cursor)
