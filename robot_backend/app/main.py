import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.router import router
from .api.body_limit import BodyLimitMiddleware
from .config import Settings
from .services.agent import Agent
from .services.companion import Companion
from .services.dedicated_server import DedicatedServer, DeliveryError
from .services.qwen import QwenClient, QwenError
from .services.deepgram import DeepgramClient, SpeechError
from .services.deepgram_live import DeepgramLive
from .services.live_audio import AudioHub
from .sessions.manager import InMemorySessionManager, SessionError

logger = logging.getLogger(__name__)


async def periodic(action, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await action()
        except Exception:
            logger.error("Background maintenance failed; will retry")


def create_app(
    settings: Settings | None = None,
    *,
    model=None,
    sink=None,
    sessions=None,
    speech=None,
    live_speech=None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            store = sessions or InMemorySessionManager(settings)
            delivery = sink or DedicatedServer(settings, client)
            companion = Companion(settings, client, delivery)
            app.state.companion = companion
            agent = Agent(
                settings,
                store,
                model or QwenClient(settings, client),
                companion,
                speech or DeepgramClient(settings, client),
            )
            app.state.agent = agent
            app.state.audio_hub = AudioHub()
            app.state.live_speech = live_speech or DeepgramLive(settings)
            tasks = [
                asyncio.create_task(
                    periodic(agent.maintain, settings.maintenance_interval_seconds)
                ),
                asyncio.create_task(
                    periodic(
                        companion.deliver_pending, settings.maintenance_interval_seconds
                    )
                ),
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task
                await store.clear()
                companion.close()
                if sink is None:
                    delivery.close()

    app = FastAPI(title="Annie Companion Robot API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 65536)
    app.include_router(router)

    @app.exception_handler(SessionError)
    async def session_error(request: Request, exc: SessionError):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message}
        )

    @app.exception_handler(QwenError)
    async def qwen_error(request: Request, exc: QwenError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(DeliveryError)
    async def delivery_error(request: Request, exc: DeliveryError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(SpeechError)
    async def speech_error(request: Request, exc: SpeechError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid request fields; see /docs for the schema"},
        )

    return app


app = create_app()
