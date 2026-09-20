import asyncio
import anyio
from contextlib import suppress
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pymongo.errors import PyMongoError
from ..schemas.common import MAX_ID

router = APIRouter()


@router.websocket('/ws')
async def updates(ws: WebSocket):
    try:
        if set(ws.query_params) != {'app_user_id'} or len(ws.query_params.getlist('app_user_id')) != 1:
            raise ValueError()
        user_id = int(ws.query_params['app_user_id'])
        if not 1 <= user_id <= MAX_ID:
            raise ValueError()
        user, _ = await ws.app.state.services.context.household(user_id)
    except (ValueError, HTTPException, PyMongoError):
        await ws.close(code=1008)
        return
    events = ws.app.state.services.context.events
    subscriber = events.subscribe(user)
    await ws.accept()

    async def send():
        await ws.send_json({'type': 'connected', 'app_user_id': user_id, 'refresh_required': True})
        while True:
            event = await subscriber.queue.get()
            await asyncio.wait_for(ws.send_json(event), timeout=10)

    async def receive():
        while True:
            message = await ws.receive()
            if message['type'] == 'websocket.disconnect':
                return
            # This socket is a read-only update stream.
            if message.get('text') or message.get('bytes'):
                await ws.close(code=1008)
                return

    tasks = {asyncio.create_task(send()), asyncio.create_task(receive())}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        events.unsubscribe(subscriber)
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
