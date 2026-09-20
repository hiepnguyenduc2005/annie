"""MongoDB-backed family API. See README.md for the replacement contract."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pymongo.errors import PyMongoError
from .config import Settings
from .database import Database
from .seed import seed
from .events import Events
from .boundary import LocalBoundary
from .schemas.common import utcnow
from .services.common import Context
from .services.users import Users
from .services.reminders import Reminders
from .services.messages import Messages
from .services.notifications import Notifications
from .services.robot import Robot
from .scheduler import Scheduler
from .api import voice, users, reminders, messages, notifications, robot_callbacks, websocket


def create_app(settings=None, *, clock=utcnow, transport=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(settings)
        await database.connect()
        client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        task = None
        try:
            await seed(database, clock)
            context = Context(database, Events(database), clock)
            robot = Robot(context, settings, client)
            scheduler = Scheduler(context, settings, robot)
            app.state.database = database
            app.state.services = SimpleNamespace(context=context, users=Users(context), reminders=Reminders(context),
                messages=Messages(context), notifications=Notifications(context), robot=robot, scheduler=scheduler)
            if settings.worker_enabled:
                task = asyncio.create_task(scheduler.run())
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
            await database.close()

    app = FastAPI(title='Annie Family API', version='2.0.0', lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(LocalBoundary)
    for module in (voice, users, reminders, messages, notifications, robot_callbacks, websocket):
        app.include_router(module.router)

    @app.exception_handler(TimeoutError)
    @app.exception_handler(PyMongoError)
    async def database_error(request, exc):
        return JSONResponse({'detail': 'Database unavailable; retry with the same Idempotency-Key'}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response can echo submitted private text or credentials.
        errors = [{'location': list(e['loc']), 'message': e['msg'], 'type': e['type']} for e in exc.errors()]
        return JSONResponse({'detail': errors}, status_code=422)

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/ready')
    async def ready():
        await app.state.database.client.admin.command('ping')
        return {'status': 'ready', 'storage': 'mongodb'}

    return app


app = create_app()
