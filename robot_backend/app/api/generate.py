from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ..schemas import GenerateResponse
from ..services.qwen import media_part
from .dependencies import phone_access

router = APIRouter(dependencies=[Depends(phone_access)])
MEDIA_TYPES = {
    "audio": {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/flac",
        "audio/ogg",
        "audio/mp4",
    },
}


async def upload_part(upload: UploadFile, kind: str, limit: int) -> dict:
    try:
        mime = (upload.content_type or "").split(";")[0].lower()
        if mime not in MEDIA_TYPES[kind]:
            raise HTTPException(415, f"Unsupported {kind} media type")
        data = await upload.read(limit + 1)
        if not data:
            raise HTTPException(400, f"Empty {kind} upload")
        if len(data) > limit:
            raise HTTPException(413, f"{kind} upload exceeds the configured limit")
        return media_part(data, mime, kind)
    finally:
        await upload.close()


@router.post("/generate", response_model=GenerateResponse)
async def generate(
    request: Request,
    session_id: str | None = Form(None, max_length=128),
    text: str | None = Form(None, max_length=8000),
    start: bool = Form(False),
    audio: UploadFile | None = File(None),
):
    form = await request.form()
    if "image" in form:
        raise HTTPException(422, "Image input is not supported by this endpoint")
    content = []
    if text and text.strip():
        content.append({"type": "text", "text": text.strip()})
    for kind, upload in (("audio", audio),):
        if upload is not None:
            content.append(
                await upload_part(
                    upload, kind, request.app.state.settings.max_upload_bytes
                )
            )
    return await request.app.state.agent.generate(session_id, content, start)
