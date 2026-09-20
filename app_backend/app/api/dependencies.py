import hmac
from typing import Annotated
from fastapi import Header, HTTPException, Request, Query
from ..schemas.common import MAX_ID

UserID = Annotated[int, Query(ge=1, le=MAX_ID)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
IdempotencyKey = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]


def services(request: Request):
    return request.app.state.services


async def robot_auth(request: Request):
    expected = request.app.state.settings.internal_secret.get_secret_value()
    given = request.headers.get('x-internal-secret', '')
    if not expected or not hmac.compare_digest(expected.encode(), given.encode()):
        raise HTTPException(401, 'Valid X-Internal-Secret required')
