from fastapi import APIRouter

from .generate import router as generate_router
from .requests import router as requests_router
from .sessions import router as sessions_router
from .audio import router as audio_router

from .companion import router as companion_router

router = APIRouter()
router.include_router(companion_router)
router.include_router(generate_router)
router.include_router(requests_router)
router.include_router(sessions_router)
router.include_router(audio_router)


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "robot"}
