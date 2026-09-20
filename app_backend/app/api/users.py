from fastapi import APIRouter, Depends, Query
from ..schemas.common import Page
from .dependencies import services, Limit, Cursor, IdempotencyKey
from ..schemas.users import NewDogUser, NewAppUser, DogUser, AppUser

router = APIRouter(prefix='/api', tags=['users'])


@router.get('/dog-users', response_model=Page[DogUser])
async def dog_users(limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.users.list('dog_users', limit, cursor)


@router.post('/dog-users', response_model=DogUser, status_code=201)
async def new_dog_user(body: NewDogUser, key: IdempotencyKey, svc=Depends(services)):
    return await svc.users.create('dog_users', body, key)


@router.get('/app-users', response_model=Page[AppUser])
async def app_users(dog_user_id: int | None = Query(default=None, ge=1), limit: Limit = 50, cursor: Cursor = None, svc=Depends(services)):
    return await svc.users.list('app_users', limit, cursor, dog_user_id)


@router.post('/app-users', response_model=AppUser, status_code=201)
async def new_app_user(body: NewAppUser, key: IdempotencyKey, svc=Depends(services)):
    return await svc.users.create('app_users', body, key)
