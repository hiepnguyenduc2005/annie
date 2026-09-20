"""Local-only, single-worker demo API. No hardware control is implemented."""
import asyncio
import contextlib
import hmac
import ipaddress
import json
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

from .companion import AskRequest, CompanionService, NewReminder
from .family import FamilyService, InternalEventIn, MessageIn
from .schema_models import (EmergencyIn, MessageReplyIn, NewAppUser, NewDogUser, NewSchemaMessage,
                            NewSchemaReminder, ReminderHistoryIn)
from .schema_store import SchemaStore, StoreError
from .models import Ack, Command, CommandReceipt, Ingest, Say, Scenario
from .service import Service, now_ms
from .subconscious_provider import DEFAULT_MODEL, SubconsciousAPIError, SubconsciousInputError, run_team


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


class DogCommandIn(BaseModel):
    """One of: a web action for the dog process, or a plain-language instruction."""
    action: str | None = Field(default=None, pattern=r'^(go_home|stop|explore|scan|dance|hello|sit|stand|follow)$')
    text: str | None = Field(default=None, min_length=1, max_length=300)
    author: str | None = Field(default=None, max_length=60)


class PersonIn(BaseModel):
    """A person the family wants Annie to know; photos are base64 JPEGs used once for the face embedding."""
    name: str = Field(min_length=1, max_length=40)
    relation: str | None = Field(default=None, max_length=40)
    shirt: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=200)
    photos: list[str] = Field(default_factory=list, max_length=10)


class VoiceSettingsIn(BaseModel):
    """Family-app voice settings; keys are forwarded to the dog process and held in memory only."""
    cloud: bool | None = None
    eleven_key: str | None = Field(default=None, max_length=200)
    deepgram_key: str | None = Field(default=None, max_length=200)
    eleven_voice: str | None = Field(default=None, max_length=80)
    input_device: str | None = Field(default=None, max_length=80)   # mic name: AirPods, MacBook Pro Microphone, DGX...
    output_device: str | None = Field(default=None, max_length=80)  # speaker name


def create_app(db_path=None, mode=None, token=None, clock=now_ms, family_service=None,
                internal_secret=None, schema_store=None):
    db_path = db_path or os.getenv('ANNIE_DB_PATH', '.data/annie.sqlite3')
    mode = mode or os.getenv('ANNIE_MODE', 'demo')
    if mode not in ('demo', 'live'):
        raise ValueError('ANNIE_MODE must be demo or live')
    token = os.getenv('ANNIE_API_TOKEN', '') if token is None else token
    internal_secret = os.getenv('ANNIE_INTERNAL_SECRET', '') if internal_secret is None else internal_secret

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(db_path, mode, clock)
        app.state.companion = CompanionService()
        def recall_provider():
            fact = app.state.companion.phone_fact()
            return fact['text'] if fact else None
        app.state.family = family_service or FamilyService(
            clock=clock,
            robot_backend_url=os.getenv('ROBOT_BACKEND_URL', ''),
            internal_secret=internal_secret,
            dispatch_timeout=float(os.getenv('ANNIE_ROBOT_DISPATCH_TIMEOUT_S', '3')),
            mock=os.getenv('ANNIE_FAMILY_MOCK_ROBOT', 'false').lower() == 'true',
            recall_provider=recall_provider,
            on_outcome=lambda run: app.state.apply_outcome(run),
            event_deadline_s=float(os.getenv('ANNIE_RUN_EVENT_DEADLINE_S', '120')),
        )
        app.state.schema = schema_store or SchemaStore()
        await app.state.schema.connect()
        app.state.agent_lock = asyncio.Lock()
        async def ticker():
            while True:
                await asyncio.sleep(.25)
                app.state.service.tick()
                app.state.family.tick()  # inactivity deadline: live runs never hang forever
        task = asyncio.create_task(ticker())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for background in list(app.state.family.background_tasks):
                background.cancel()
            await app.state.schema.close()
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

    async def authorize_internal(request: Request):
        # Separate trust domain from `authorize`: robot_backend calls this from
        # a different LAN host, never with the family ANNIE_API_TOKEN. An
        # unconfigured secret must reject everything, never fall open.
        given = request.headers.get('x-internal-secret', '')
        if not internal_secret or not hmac.compare_digest(given.encode(), internal_secret.encode()):
            raise HTTPException(401, 'Valid X-Internal-Secret header required')

    router = APIRouter(dependencies=[Depends(authorize)])
    internal_router = APIRouter(dependencies=[Depends(authorize_internal)])

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

    @router.post('/api/messages', status_code=202)
    async def post_message(body: MessageIn):
        if app.state.family.pausing:
            raise HTTPException(409, 'Pause is still being confirmed; send a new request afterwards')
        # Must never block on robot_backend: the dispatch runs as a background
        # task started inside post_message, so this returns immediately.
        # An exact repeat of a still-active message returns the existing run instead
        # of piling a duplicate errand onto the dog's queue.
        _message, run, created = app.state.family.post_message(body.author_id, body.text,
                                                               reminder_id=body.reminder_id)
        receipt = {'run_id': run['run_id'], 'status': run['status']}
        if not created:
            receipt['deduplicated'] = True
        return receipt

    @router.post('/api/family/pause')
    async def family_pause():
        """Pause everything family-triggered: live runs become cancelled (terminal), their
        in-process tasks stop, and the errand service is told to cancel its active errand,
        clear its queue and issue the software body stop. stop_confirmed=false means that
        stop was not acknowledged — treat the dog's motion state as unknown. Resume by
        sending a new explicit message; nothing replays automatically."""
        return await app.state.family.pause()

    @router.get('/api/runs/{run_id}')
    async def get_run(run_id: UUID):
        run = app.state.family.runs.get(str(run_id))
        if run is None:
            raise HTTPException(404, 'Unknown run')
        return run

    @router.get('/api/thread')
    async def get_thread():
        return app.state.family.thread

    @router.get('/api/family/snapshot')
    async def family_snapshot():
        """One bounded, consistent refresh for family clients, including other phones' runs."""
        return app.state.family.recent_history()

    @router.get('/api/reminders')
    async def list_reminders():
        return app.state.companion.reminders

    @router.post('/api/reminders')
    async def create_reminder(body: NewReminder):
        return app.state.companion.add_reminder(body.time, body.title)

    @router.patch('/api/reminders/{reminder_id}/toggle')
    async def toggle_reminder(reminder_id: int):
        try:
            return app.state.companion.toggle_reminder(reminder_id)
        except KeyError:
            raise HTTPException(404, 'Unknown reminder') from None

    async def _merge_dog_memory():
        """Best effort: the dog process' telemetry (its space-time graph) becomes live history facts."""
        url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/')
        if not url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(url + '/telemetry.json')
            if r.status_code == 200:
                app.state.companion.merge_live(r.json())
        except Exception as exc:  # dog process down: seeded/added facts only
            app.state.dog_memory_error = f"{type(exc).__name__}: {exc}"[:200]
            return

    async def _dog_get(path: str, timeout=2.0):
        url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/')
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(url + path)

    def _dog_headers():
        return {'X-Body-Token': os.getenv('ANNIE_BODY_TOKEN', '')} if os.getenv('ANNIE_BODY_TOKEN') else {}

    @router.get('/api/dog/status')
    async def dog_status():
        """The dog process' telemetry for the app's control card (mode, battery, people, missions, voice)."""
        try:
            r = await _dog_get('/telemetry.json')
            if r.status_code == 200:
                t = r.json()
                st = t.get('state') or {}
                return {'available': True, 'connected': bool(t.get('connected')), 'mode': st.get('mode'), 'action': st.get('action'),
                        'battery': st.get('battery'), 'people': len(st.get('tracks') or []), 'greetings': st.get('greetings'),
                        'checkins': st.get('checkins'), 'missions': (t.get('missions') or [])[:6], 'voice': t.get('voice') or {},
                        'objects': t.get('objects') or [], 'sentences': (t.get('graph_sentences') or [])[:4], 't_s': st.get('t_s'),
                        'source': t.get('source') or 'hardware', 'conversations': (t.get('conversations') or [])[-8:],
                        'concerns': (t.get('concerns') or [])[-4:], 'instructions': (t.get('instructions') or [])[-6:],
                        'motion_enabled': t.get('motion_enabled'), 'paused': t.get('paused')}
        except Exception:
            pass
        return {'available': False, 'connected': False, 'motion_enabled': None, 'paused': None}

    @router.post('/api/dog/command')
    async def dog_command(body: DogCommandIn):
        """Family-app controls -> the dog process. A web action (explore, go_home, stop, scan, hello, dance) or a
        plain-language instruction (planned by the situated agent); the receipt comes back as-is."""
        url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/')
        if not body.text and not body.action:
            raise HTTPException(400, 'action or text is required')
        payload = {'name': 'instruct', 'args': {'text': body.text, 'author': body.author or 'family'}} if body.text else {'action': body.action}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.post(url + '/command', json=payload, headers=_dog_headers())
        except Exception:
            raise HTTPException(503, 'The dog process is not reachable') from None
        if r.status_code >= 400:
            raise HTTPException(r.status_code if r.status_code in (400, 401, 409) else 502, r.text[:200])
        return {'available': True, 'status_code': r.status_code, **(r.json() if r.content else {})}

    @router.get('/api/people')
    async def list_people():
        """People Annie knows (name, relation, shirt colour, how many face samples); from the dog process."""
        try:
            r = await _dog_get('/people')
            if r.status_code == 200:
                return {'available': True, **r.json()}
        except Exception:
            pass
        return {'available': False, 'people': []}

    @router.post('/api/people')
    async def add_person(body: PersonIn):
        """Enrol a person from the family app: name, relation, optional shirt colour and up to 10 photos (base64
        JPEG). Photos go to the dog process for the face embedding and are not kept anywhere."""
        url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/')
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url + '/people', json=payload, headers=_dog_headers())
        except Exception:
            raise HTTPException(503, 'The dog process is not reachable') from None
        if r.status_code == 400:
            raise HTTPException(400, (r.json() or {}).get('error', 'invalid person'))
        if r.status_code != 200:
            raise HTTPException(502, 'The dog process could not enrol the person')
        return {'available': True, **r.json()}

    @router.delete('/api/people/{name}')
    async def forget_person(name: str):
        url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/')
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(url + '/people/forget', json={'name': name}, headers=_dog_headers())
        except Exception:
            raise HTTPException(503, 'The dog process is not reachable') from None
        if r.status_code == 404:
            raise HTTPException(404, 'Unknown person')
        return {'available': True, **r.json()}

    @router.get('/api/settings/voice')
    async def voice_settings():
        """How Annie speaks and hears right now (from the dog process); keys are never returned."""
        try:
            r = await _dog_get('/voice')
            if r.status_code == 200:
                return {'available': True, **r.json()}
        except Exception:
            pass
        return {'available': False, 'cloud': False, 'elevenlabs': False, 'deepgram': False, 'speak_via': 'off', 'hear_via': 'off'}

    @router.post('/api/settings/voice')
    async def set_voice_settings(body: VoiceSettingsIn):
        """Family-app switch for cloud voice (ElevenLabs speaks, Deepgram hears), optional keys and the mic/speaker
        device names, forwarded to the dog process and held there in memory; nothing is written or logged here."""
        url = os.getenv('ANNIE_DOG_VIEW_URL', 'http://127.0.0.1:8011').rstrip('/')
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.post(url + '/voice', json=payload, headers=_dog_headers())
        except Exception:
            raise HTTPException(503, 'The dog process is not reachable') from None
        if r.status_code != 200:
            raise HTTPException(502, 'The dog process refused the voice settings')
        return {'available': True, **r.json()}

    @router.get('/api/memory')
    async def list_memory():
        await _merge_dog_memory()
        return app.state.companion.all_memory()

    @router.post('/api/ask')
    async def ask_annie(body: AskRequest):
        await _merge_dog_memory()
        return {'answer': app.state.companion.ask(body.question)}

    @internal_router.post('/internal/events', status_code=202)
    async def internal_event(body: InternalEventIn):
        try:
            return app.state.family.add_event(str(body.run_id), body.kind, body.payload, body.at)
        except KeyError:
            raise HTTPException(404, 'Unknown run') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    # ---- household schema: profiles, messages, reminders, history, emergencies ----

    def _apply_outcome(run):
        """Reminder update: an acknowledged reminder is marked done. Emergency: recorded as a live alert fact."""
        outcome = run.get('outcome') or {}
        companion = app.state.companion
        if outcome.get('type') == 'reminder_update' and outcome.get('done') and outcome.get('reminder_id') is not None:
            with contextlib.suppress(KeyError):
                item = next((r for r in companion.reminders if r['id'] == outcome['reminder_id']), None)
                if item is not None and not item['done']:
                    companion.toggle_reminder(outcome['reminder_id'])
        if outcome.get('type') == 'emergency':
            companion.alerts.append({'run_id': run['run_id'], 'at': run.get('updated_at'), 'detail': outcome.get('detail', '')})
            del companion.alerts[:-10]

    app.state.apply_outcome = _apply_outcome  # attached to the family service once the lifespan creates it

    @router.get('/api/alerts')
    async def list_alerts():
        """Emergencies raised by missions (a reply that sounded like a call for help), newest last."""
        return app.state.companion.alerts

    def store():
        return app.state.schema

    @router.get('/api/storage')
    async def storage_status():
        """Whether records are persisting to MongoDB or the in-memory fallback."""
        return store().status()

    @router.post('/api/dog-users', status_code=201)
    async def create_dog_user(body: NewDogUser):
        try:
            return await store().create_dog_user(body.name, body.id)
        except StoreError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get('/api/dog-users')
    async def list_dog_users():
        return await store().dog_users()

    @router.post('/api/app-users', status_code=201)
    async def create_app_user(body: NewAppUser):
        try:
            return await store().create_app_user(body.name, body.dog_user_id)
        except StoreError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get('/api/app-users')
    async def list_app_users(dog_user_id: str | None = None):
        return await store().app_users(dog_user_id)

    @router.post('/api/schema/messages', status_code=201)
    async def create_schema_message(body: NewSchemaMessage):
        """Record a message and hand its text to robot_backend. The robot's
        summary comes back to /internal/message-reply and joins `texts`."""
        try:
            message = await store().create_message(body.dog_user_id, body.app_user_id, body.text)
        except StoreError as exc:
            raise HTTPException(422, str(exc)) from None
        app.state.family.dispatch_schema_message(message)
        return message

    @router.get('/api/schema/messages')
    async def list_schema_messages(dog_user_id: str | None = None):
        return await store().messages(dog_user_id)

    @router.post('/api/schema/reminders', status_code=201)
    async def create_schema_reminder(body: NewSchemaReminder):
        try:
            reminder = await store().create_reminder(body.dog_user_id, body.hour, body.item)
        except StoreError as exc:
            raise HTTPException(422, str(exc)) from None
        app.state.family.dispatch_reminder(reminder)
        return reminder

    @router.get('/api/schema/reminders')
    async def list_schema_reminders(dog_user_id: str | None = None):
        return await store().reminders(dog_user_id)

    @router.get('/api/schema/reminders/{reminder_id}/history')
    async def list_history(reminder_id: str):
        if not await store().reminder(reminder_id):
            raise HTTPException(404, 'Unknown reminder')
        return await store().history_records(reminder_id)

    @router.get('/api/schema/history')
    async def list_all_history():
        return await store().history_records()

    @router.get('/api/schema/emergencies')
    async def list_emergencies(dog_user_id: str | None = None):
        return await store().emergencies(dog_user_id)

    # ---- inbound from robot_backend (shared secret, not the family token) ----

    @internal_router.post('/internal/message-reply', status_code=202)
    async def message_reply(body: MessageReplyIn):
        try:
            entry = await store().append_message_text(body.message_id, body.text, body.source)
        except StoreError as exc:
            raise HTTPException(404, str(exc)) from None
        app.state.family.emit('schema_message_reply',
                              {'message_id': body.message_id, **entry})
        return entry

    @internal_router.post('/internal/reminder-history', status_code=202)
    async def reminder_history(body: ReminderHistoryIn):
        try:
            record = await store().create_history_record(body.reminder_id, body.description, body.timedate)
        except StoreError as exc:
            raise HTTPException(404, str(exc)) from None
        app.state.family.emit('history_record', record)
        return record

    @internal_router.post('/internal/emergencies', status_code=202)
    async def emergency(body: EmergencyIn):
        try:
            record = await store().create_emergency(body.dog_user_id, body.description, body.timestamp)
        except StoreError as exc:
            raise HTTPException(422, str(exc)) from None
        # Emergencies are what the family most needs to see immediately.
        app.state.family.emit('emergency', record)
        return record

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

    app.include_router(router)
    app.include_router(internal_router)

    async def ws_authenticate(ws: WebSocket) -> bool:
        scheme = 'https' if ws.url.scheme == 'wss' else 'http'
        if ws.query_params or not same_origin(ws.headers, scheme):
            await ws.close(code=1008)
            return False
        if not token and (not ws.client or not loopback(ws.client.host)):
            await ws.close(code=1008)
            return False
        await ws.accept()
        if token:
            try:
                message = await asyncio.wait_for(ws.receive_text(), timeout=5)
                if len(message) > 4096:
                    raise ValueError()
                auth = json.loads(message)
                if not isinstance(auth, dict) or set(auth) != {'token'} or not isinstance(auth['token'], str) or not hmac.compare_digest(auth['token'].encode(), token.encode()):
                    raise ValueError()
            except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
                await ws.close(code=1008)
                return False
        return True

    async def ws_pump(ws: WebSocket, queue: asyncio.Queue):
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

    @app.websocket('/live')
    async def live(ws: WebSocket):
        if not await ws_authenticate(ws):
            return
        service = app.state.service
        queue = asyncio.Queue(maxsize=100)
        service.subscribers.add(queue)
        try:
            await ws.send_json({'type': 'snapshot', 'data': service.status()})
            await ws_pump(ws, queue)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            service.subscribers.discard(queue)

    @app.websocket('/ws/family')
    async def ws_family(ws: WebSocket):
        if not await ws_authenticate(ws):
            return
        family = app.state.family
        queue = asyncio.Queue(maxsize=100)
        family.subscribers.add(queue)
        try:
            await ws.send_json({'type': 'snapshot', 'data': family.recent_history()})
            await ws_pump(ws, queue)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            family.subscribers.discard(queue)

    static = Path(__file__).resolve().parents[2] / 'frontend'
    if static.is_dir():
        app.mount('/app', StaticFiles(directory=static, html=True), name='app')
    return app


app = create_app()
