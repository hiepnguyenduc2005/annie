from fastapi.testclient import TestClient
from pymongo import MongoClient
from starlette.websockets import WebSocketDisconnect
import pytest
from app.main import create_app


def test_websocket_shared_reminders_and_private_chat(settings, clock):
    app = create_app(settings, clock=clock)
    try:
        with TestClient(app) as client:
            with client.websocket_connect('/ws?app_user_id=2') as zach, client.websocket_connect('/ws?app_user_id=3') as ellis:
                assert zach.receive_json()['type'] == 'connected'
                assert ellis.receive_json()['type'] == 'connected'
                result = client.post('/api/reminders', json={'app_user_id': 2, 'daily_time': '17:00', 'description': 'Hello'},
                                     headers={'Idempotency-Key': 'shared'})
                assert result.status_code == 201
                assert zach.receive_json()['type'] == ellis.receive_json()['type'] == 'reminder.created'
                result = client.post('/api/messages', json={'app_user_id': 2, 'text': 'Private conversation'}, headers={'Idempotency-Key': 'private'})
                assert result.status_code == 202
                assert zach.receive_json()['type'] == 'message.created'
                client.post('/api/reminders', json={'app_user_id': 3, 'daily_time': '17:30', 'description': 'Next'},
                            headers={'Idempotency-Key': 'shared2'})
                assert ellis.receive_json()['type'] == 'reminder.created'  # No private message leaked.
            assert len(app.state.services.context.events.subscribers) == 0
            for url in ('/ws?app_user_id=999', '/ws?app_user_id=2&token=oops'):
                with pytest.raises(WebSocketDisconnect):
                    with client.websocket_connect(url):
                        pass
    finally:
        with MongoClient('mongodb://127.0.0.1:27029/?replicaSet=annieTest') as mongo:
            mongo.drop_database(settings.mongodb_db)


@pytest.mark.asyncio
async def test_real_uvicorn_websocket_upgrade(settings, clock):
    """Exercise the installed Uvicorn transport, not only TestClient's ASGI shim."""
    import asyncio
    import json
    import socket
    import httpx
    import uvicorn
    from websockets.asyncio.client import connect
    from pymongo import AsyncMongoClient
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    app = create_app(settings, clock=clock)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, proxy_headers=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                pytest.fail('Server exited before startup')
            await asyncio.sleep(.05)
        assert server.started
        async with connect(f'ws://127.0.0.1:{port}/ws?app_user_id=2', proxy=None) as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))['type'] == 'connected'
            async with httpx.AsyncClient() as client:
                response = await client.post(f'http://127.0.0.1:{port}/api/reminders',
                    json={'app_user_id': 3, 'daily_time': '19:30', 'description': 'Live socket test'},
                    headers={'Idempotency-Key': 'real-ws'})
                assert response.status_code == 201
            event = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert event['type'] == 'reminder.created'
            assert event['data']['id'] == response.json()['id']
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
        mongo = AsyncMongoClient('mongodb://127.0.0.1:27029/?replicaSet=annieTest')
        await mongo.drop_database(settings.mongodb_db)
        await mongo.close()
