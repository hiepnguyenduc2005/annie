from fastapi import APIRouter, Depends, Request

from ..schemas import Acknowledgment, RemoteRequest
from .dependencies import server_access

router = APIRouter(dependencies=[Depends(server_access)])


@router.post("/requests", response_model=Acknowledgment, status_code=202)
async def remote_request(body: RemoteRequest, request: Request):
    acknowledgment = await request.app.state.agent.request(body)
    if not acknowledgment.done:
        request.app.state.audio_hub.offer(acknowledgment.session_id)
    return acknowledgment
