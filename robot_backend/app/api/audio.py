import hmac

from fastapi import APIRouter, WebSocket

from ..services.live_audio import AudioConversation

router = APIRouter()


@router.websocket("/audio")
async def audio(websocket: WebSocket):
    expected = websocket.app.state.settings.phone_api_key.get_secret_value()
    if expected and not hmac.compare_digest(
        websocket.headers.get("authorization", "").encode(),
        ("Bearer " + expected).encode(),
    ):
        await websocket.close(1008)
        return
    hub = websocket.app.state.audio_hub
    if hub.connection is not None:
        await websocket.close(1013)
        return
    connection = AudioConversation(
        websocket, websocket.app.state.agent, websocket.app.state.live_speech, hub
    )
    hub.connection = connection
    try:
        await websocket.accept()
        await connection.run()
    finally:
        hub.connection = None
