from fastapi import APIRouter, Depends, Request

from ..schemas import EndRequest, EndResponse
from .dependencies import phone_access

router = APIRouter(dependencies=[Depends(phone_access)])


@router.post("/sessions/{session_id}/end", response_model=EndResponse)
async def end_session(
    session_id: str, request: Request, body: EndRequest | None = None
):
    await request.app.state.agent.end(session_id, body.reason if body else "ended")
    return EndResponse(session_id=session_id)
