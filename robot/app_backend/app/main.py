"""Local-only, single-worker demo API. No hardware control is implemented."""
import asyncio
import contextlib
import hmac
import ipaddress
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import Ack, Command, CommandReceipt, Ingest, Say, Scenario
from .service import Service, now_ms
from .subconscious_provider import DEFAULT_MODEL, SubconsciousAPIError, SubconsciousInputError, run_team
from .audio_reply import build_audio_reply_router


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task: str = Field(min_length=1, max_length=4000)
    evidence: list[dict] = Field(default_factory=list, max_length=12)
    allow_cloud: bool = False


def loopback(host):
    if host == 'testclient':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def same_origin(headers, scheme):
    origin = headers.get('origin')
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme == scheme and parsed.netloc == headers.get('host') and not parsed.path and not parsed.query and not parsed.fragment


def create_app(db_path=None, mode=None, token=None, clock=now_ms, require_audio_receipt=None):
    db_path = db_path or os.getenv('ANNIE_DB_PATH', '.data/annie.sqlite3')
    mode = mode or os.getenv('ANNIE_MODE', 'demo')
    if mode not in ('demo', 'live'):
        raise ValueError('ANNIE_MODE must be demo or live')
    token = os.getenv('ANNIE_API_TOKEN', '') if token is None else token
    if require_audio_receipt is None:
        require_audio_receipt = os.getenv('ANNIE_REQUIRE_AUDIO_RECEIPT', 'false').lower() == 'true'

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(db_path, mode, clock, require_audio_receipt)
        app.state.agent_lock = asyncio.Lock()
        async def ticker():
            while True:
                await asyncio.sleep(.25)
                app.state.service.tick()
        task = asyncio.create_task(ticker())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.service.close()

    app = FastAPI(title='Annie API', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Restrict unauthenticated loopback demos against DNS rebinding. Token-based
    # LAN operation may set an explicit host list without weakening token auth.
    hosts = os.getenv('ANNIE_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver').split(',')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host.strip() for host in hosts if host.strip()])

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if not same_origin(request.headers, request.url.scheme):
            return JSONResponse({'detail': 'Cross-origin requests are not allowed'}, status_code=403)
        # Bound streamed bodies too, before Pydantic/JSON allocates unbounded input.
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length) > 600000):
            return JSONResponse({'detail': 'Request body too large'}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 600000:
                return JSONResponse({'detail': 'Request body too large'}, status_code=413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    async def authorize(request: Request):
        if token:
            given = request.headers.get('authorization', '')
            if not hmac.compare_digest(given.encode(), ('Bearer ' + token).encode()):
                raise HTTPException(401, 'Bearer token required')
        elif not request.client or not loopback(request.client.host):
            raise HTTPException(403, 'Without ANNIE_API_TOKEN only loopback clients are allowed')

    router = APIRouter(dependencies=[Depends(authorize)])

    @app.get('/')
    async def root():
        return {'message': 'Welcome to the Annie API'}

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @router.get('/status')
    async def status():
        return app.state.service.status()

    @router.get('/openapi.json', include_in_schema=False)
    async def openapi_schema():
        return app.openapi()

    @router.post('/agents/run')
    async def agents(body: AgentRequest):
        if not body.allow_cloud:
            raise HTTPException(422, 'Set allow_cloud:true to send the supplied text evidence to Subconscious')
        key = os.getenv('SUBCONSCIOUS_API_KEY', '')
        if os.getenv('ANNIE_ENABLE_CLOUD_AGENTS', 'false').lower() != 'true' or not key:
            raise HTTPException(503, 'Subconscious is disabled or not configured')
        if app.state.agent_lock.locked():
            raise HTTPException(429, 'An advisory team is already running')
        try:
            async with app.state.agent_lock:
                return await run_team(body.task, body.evidence, api_key=key,
                                      model=os.getenv('SUBCONSCIOUS_MODEL', DEFAULT_MODEL))
        except SubconsciousInputError as exc:
            raise HTTPException(422, str(exc)) from None
        except SubconsciousAPIError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.get('/map')
    async def get_map():
        if app.state.service.map is None:
            raise HTTPException(404, 'No map received')
        return app.state.service.map

    @router.get('/events')
    async def events(since: int = 0):
        if since < 0:
            raise HTTPException(422, 'since must be a nonnegative Unix millisecond timestamp')
        return app.state.service.events(since)

    @router.post('/events/{event_id}/ack')
    async def ack(event_id: UUID, body: Ack):
        try:
            return app.state.service.ack(str(event_id), body.by)
        except KeyError:
            raise HTTPException(404, 'Unknown event')

    @router.post('/say')
    async def say(body: Say):
        return app.state.service.queue_command({'cmd': 'say', 'text': body.text})

    @router.get('/commands')
    async def commands():
        return app.state.service.commands

    @router.post('/commands')
    async def command(body: Command):
        if body.cmd == 'goto':
            map_data = app.state.service.map
            if not map_data or body.waypoint not in {item['id'] for item in map_data['waypoints']}:
                raise HTTPException(422, 'Unknown waypoint')
        elif body.waypoint is not None:
            raise HTTPException(422, 'waypoint is only valid with goto')
        return app.state.service.queue_command(body.model_dump())

    @router.post('/commands/{command_id}/receipt')
    async def command_receipt(command_id: UUID, body: CommandReceipt):
        try:
            return app.state.service.command_receipt(str(command_id), body.status, body.source, body.detail)
        except KeyError:
            raise HTTPException(404, 'Unknown command') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post('/query')
    async def query(body: Say):
        return app.state.service.query(body.text)

    @router.get('/frames/{frame_id}')
    async def frame(frame_id: UUID):
        crop = app.state.service.crops.get(str(frame_id))
        if crop is None:
            raise HTTPException(404, 'No crop stored for this frame')
        return Response(crop, media_type='image/jpeg')

    @router.post('/ingest')
    async def ingest(body: Ingest):
        try:
            accepted = app.state.service.ingest(body.channel, body.data)
        except ValidationError:
            raise HTTPException(422, 'Invalid channel payload; consult contract/schemas.json')
        return {'accepted': accepted}

    def demo_only():
        if mode != 'demo':
            raise HTTPException(403, 'Demo controls are unavailable in live mode')

    @router.post('/demo/seed', dependencies=[Depends(demo_only)])
    async def seed():
        app.state.service.seed()
        return app.state.service.status()

    @router.post('/demo/scenario', dependencies=[Depends(demo_only)])
    async def scenario(body: Scenario):
        app.state.service.scenario(body.scenario)
        return app.state.service.status()

    @router.post('/demo/reset-episode', dependencies=[Depends(demo_only)])
    async def reset_episode():
        try:
            return app.state.service.reset_demo_episode()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    app.include_router(router)
    app.include_router(build_audio_reply_router(lambda: app.state.service, authorize))

    @app.websocket('/live')
    async def live(ws: WebSocket):
        scheme = 'https' if ws.url.scheme == 'wss' else 'http'
        if ws.query_params or not same_origin(ws.headers, scheme):
            await ws.close(code=1008)
            return
        if not token and (not ws.client or not loopback(ws.client.host)):
            await ws.close(code=1008)
            return
        await ws.accept()
        if token:
            try:
                message = await asyncio.wait_for(ws.receive_text(), timeout=5)
                if len(message) > 4096:
                    raise ValueError()
                import json
                auth = json.loads(message)
                if not isinstance(auth, dict) or set(auth) != {'token'} or not isinstance(auth['token'], str) or not hmac.compare_digest(auth['token'].encode(), token.encode()):
                    raise ValueError()
            except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
                await ws.close(code=1008)
                return
        service = app.state.service
        queue = asyncio.Queue(maxsize=100)
        service.subscribers.add(queue)
        try:
            await ws.send_json({'type': 'snapshot', 'data': service.status()})
            # Receive concurrently so disconnected clients don't retain queues indefinitely.
            async def send_updates():
                while True:
                    await ws.send_json(await queue.get())
            async def receive_disconnect():
                while True:
                    message = await ws.receive()
                    if message['type'] == 'websocket.disconnect':
                        return
                    await ws.close(code=1008)
                    return
            tasks = {asyncio.create_task(send_updates()), asyncio.create_task(receive_disconnect())}
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            service.subscribers.discard(queue)

    static = Path(__file__).resolve().parents[2] / 'frontend'
    if static.is_dir():
        app.mount('/app', StaticFiles(directory=static, html=True), name='app')
    return app


app = create_app()
