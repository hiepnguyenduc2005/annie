"""Run with uvicorn robot.robot_backend.app.brain.api:app --port 8002."""
import asyncio
import hmac
import ipaddress
import os
import time
from dataclasses import replace
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from robot.app_backend.app.models import Perception
from .models import FrameRequest, InferenceResult, MAX_BODY_BYTES, ProviderMetadata
from .provider import InvalidFrame, ProviderError, ProviderTimeout, VisionConfig, infer_image, sanitize_jpeg
from .budget import BudgetError


def create_app(config: VisionConfig | None = None, *, token: str | None = None, transport=None,
               audio_config: VisionConfig | None = None):
    config = config if config is not None else VisionConfig.from_env()
    if audio_config is None and os.getenv('ANNIE_AUDIO_ENABLED', 'false').lower() == 'true':
        # Audio is configured independently of local image inference. It never
        # silently converts a local vision request into cloud egress.
        audio_config = replace(config, mode='cloud', base_url='https://openrouter.ai/api/v1',
                               model='xiaomi/mimo-v2.5:floor',
                               api_key=os.getenv('OPENROUTER_API_KEY', ''), timeout_s=30)
    token = os.getenv('ANNIE_BRAIN_TOKEN', os.getenv('ANNIE_API_TOKEN', '')) if token is None else token
    app = FastAPI(title='Annie GX10 brain stand-in', docs_url=None, redoc_url=None, openapi_url=None)
    lock = asyncio.Lock()
    hosts = os.getenv('ANNIE_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver').split(',')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host.strip() for host in hosts if host.strip()])

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        body_limit = 5_340_000 if request.url.path == '/transcribe' else MAX_BODY_BYTES
        origin = request.headers.get('origin')
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                same = (parsed.scheme == request.url.scheme and parsed.netloc == request.headers.get('host')
                        and not parsed.path and not parsed.query and not parsed.fragment)
            except ValueError:
                same = False
            if not same:
                return JSONResponse({'detail': 'Cross-origin requests are not allowed'}, status_code=403)
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length) > body_limit):
            return JSONResponse({'detail': 'Request body too large'}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > body_limit:
                return JSONResponse({'detail': 'Request body too large'}, status_code=413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        # FastAPI's default errors echo input values, including whole frames.
        return JSONResponse({'detail': 'Invalid frame request; consult contract/brain.md'}, status_code=422)

    async def authorize(request: Request):
        if token:
            given = request.headers.get('authorization', '')
            if not hmac.compare_digest(given.encode(), ('Bearer ' + token).encode()):
                raise HTTPException(401, 'Bearer token required')
        else:
            try:
                host = request.client.host if request.client else ''
                local = host == 'testclient' or ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if not local:
                raise HTTPException(403, 'Without ANNIE_API_TOKEN only loopback clients are allowed')

    @app.get('/health', dependencies=[Depends(authorize)])
    @app.get('/health/config', dependencies=[Depends(authorize)])
    async def health():
        return {'status': 'ok', 'mode': config.mode, 'configured': config.mode != 'disabled',
                'model': config.model or None, 'provider_protocol': 'chat_completions',
                'timeout_s': config.timeout_s, 'cloud_sources': ['simulation_render'],
                'max_jpeg_bytes': 1000000, 'max_dimension_px': 1280,
                'audio_model': 'xiaomi/mimo-v2.5:floor' if (audio_config or config).is_openrouter else None}

    @app.post('/infer', response_model=InferenceResult, response_model_exclude_none=True,
              dependencies=[Depends(authorize)])
    async def infer(frame: FrameRequest):
        if config.mode == 'disabled':
            raise HTTPException(503, 'Vision provider is disabled')
        if config.mode == 'cloud' and frame.source != 'simulation_render':
            raise HTTPException(403, 'Cloud vision accepts simulation_render frames only')
        if lock.locked():
            raise HTTPException(429, 'Vision inference is already running')
        started = time.perf_counter()
        try:
            async with lock:
                jpeg = await asyncio.to_thread(sanitize_jpeg, frame.jpeg_b64,
                                               config.resize_longest_side)
                result = await infer_image(config, jpeg, transport=transport)
        except InvalidFrame as exc:
            raise HTTPException(422, str(exc)) from None
        except BudgetError as exc:
            raise HTTPException(503, str(exc)) from None
        except ProviderTimeout as exc:
            raise HTTPException(504, str(exc)) from None
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from None
        perception = Perception.model_validate({**result.observation.model_dump(), 'ts': frame.ts,
                                               'frame_id': frame.frame_id, 'pose': frame.pose.model_dump(),
                                               'source': 'simulation_vlm' if frame.source == 'simulation_render' else 'hardware_vlm',
                                               'model': config.model})
        return InferenceResult(perception=perception, source=frame.source,
                               provider=ProviderMetadata(mode=config.mode, model=config.model, usage=result.usage),
                               latency_ms=round((time.perf_counter() - started) * 1000, 3))

    from .audio import build_audio_router
    app.include_router(build_audio_router(audio_config or config, transport=transport), dependencies=[Depends(authorize)])
    return app


app = create_app()
