#!/usr/bin/env python3
"""Loopback OpenAI-shaped chat server backed by mlx-vlm, for the brain's local mode.

Why: on this M1 Max the same Qwen3-VL 2B answers a never-seen frame in ~0.48 s
p50 under mlx-vlm versus ~3.0 s under Ollama (docs/VISION_BENCHMARK.md, runtime
comparison). The brain already speaks the OpenAI chat shape to a loopback URL,
so this server lets `ANNIE_VISION_BASE_URL=http://127.0.0.1:8090/v1` switch
runtimes with no brain code change.

Scope and limits:
- Accepts exactly what the brain sends: a system text, one user text and one
  `data:image/jpeg;base64,...` image. Anything else is 422. Remote image URLs
  are refused; nothing is fetched.
- One generation at a time (the model is not re-entrant); callers queue.
- `response_format` is accepted and ignored: mlx-vlm has no grammar-constrained
  decoding. The first balanced `{...}` object in the reply is returned as the
  message content so a chatty model still yields JSON; a reply without one is
  returned raw with finish_reason `length` and the brain rejects it (no
  synthetic observation is ever produced here).
- Frames stay in process. Nothing is logged except request timing.

Run from the repository root (weights come from the Hugging Face cache):
  .cache/mlx-vlm/.venv/bin/python robot/robot_backend/mlx_vision_server.py \
      --model mlx-community/Qwen3-VL-2B-Instruct-4bit --port 8090
"""
from __future__ import annotations

import argparse
import base64
import binascii
import logging
import sys
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_MODEL = "mlx-community/Qwen3-VL-2B-Instruct-4bit"
MAX_IMAGE_B64 = 2_000_000
MAX_TEXT_CHARS = 8000
LOG = logging.getLogger("mlx-vision")


def extract_json(text: str) -> tuple[str, bool]:
    """Return (first balanced JSON object in text, found). Braces inside strings are honored."""
    start = text.find("{")
    if start < 0:
        return text, False
    depth, in_string, escape = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1], True
    return text, False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(default="", max_length=200)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=8)
    max_tokens: int = Field(default=128, ge=1, le=1024)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


def _parse_messages(messages: list[dict]) -> tuple[str, str]:
    """Flatten system/user text into one prompt; return (prompt, image data URL)."""
    texts: list[str] = []
    image: str | None = None
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role not in ("system", "user"):
            raise HTTPException(422, "only system and user messages are supported")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for part in parts:
            if not isinstance(part, dict):
                raise HTTPException(422, "malformed message content")
            if part.get("type") == "text":
                text = part.get("text")
                if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
                    raise HTTPException(422, "text part must be a bounded string")
                texts.append(text)
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url")
                if image is not None:
                    raise HTTPException(422, "exactly one image is supported")
                if not isinstance(url, str) or not url.startswith("data:image/jpeg;base64,"):
                    raise HTTPException(422, "image must be an inline data:image/jpeg;base64 URL")
                encoded = url.split(",", 1)[1]
                if len(encoded) > MAX_IMAGE_B64:
                    raise HTTPException(413, "image too large")
                try:
                    base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError):
                    raise HTTPException(422, "image is not valid base64") from None
                image = url
            else:
                raise HTTPException(422, "unsupported content part")
    if image is None:
        raise HTTPException(422, "an image is required")
    if not texts:
        raise HTTPException(422, "a text prompt is required")
    return "\n\n".join(texts), image


class MlxEngine:
    """Real mlx-vlm backend; constructed only by main(), never by tests."""

    def __init__(self, model_id: str, *, revision: str | None = None, image_tokens: str = "default"):
        import mlx_vlm
        from mlx_vlm import generate, load
        from mlx_vlm.prompt_utils import apply_chat_template

        started = time.monotonic()
        self.model, self.processor = load(model_id, revision=revision)
        self.model_id = model_id
        self.runtime_version = mlx_vlm.__version__
        self._generate = generate
        self._template = apply_chat_template
        if image_tokens == "min":
            ip = getattr(self.processor, "image_processor", self.processor)
            if hasattr(ip, "min_pixels") and hasattr(ip, "max_pixels"):
                ip.max_pixels = ip.min_pixels
        self.load_s = round(time.monotonic() - started, 2)

    def generate(self, prompt: str, image_data_url: str, max_tokens: int) -> dict:
        formatted = self._template(self.processor, self.model.config, prompt, num_images=1,
                                   enable_thinking=False)
        result = self._generate(self.model, self.processor, formatted, image=[image_data_url],
                                max_tokens=max_tokens, temperature=0.0, verbose=False)
        return {"text": result.text, "prompt_tokens": int(result.prompt_tokens),
                "generation_tokens": int(result.generation_tokens),
                "finished": int(result.generation_tokens) < max_tokens}


def create_app(engine) -> FastAPI:
    app = FastAPI(title="Annie MLX vision server")
    lock = threading.Lock()
    app.state.engine = engine

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": engine.model_id, "runtime": "mlx-vlm", "ready": True}

    @app.post("/v1/chat/completions")
    async def chat(request: ChatRequest):
        prompt, image = _parse_messages(request.messages)
        started = time.perf_counter()
        # One generation at a time; run in a worker thread so the loop stays responsive.
        import asyncio

        def run():
            with lock:
                return engine.generate(prompt, image, request.max_tokens)

        try:
            result = await asyncio.to_thread(run)
        except Exception as exc:  # model failure: report type only, never the frame
            LOG.warning("generation failed: %s", type(exc).__name__)
            raise HTTPException(502, f"generation failed: {type(exc).__name__}") from None
        text = result.get("text", "")
        content, found = extract_json(text if isinstance(text, str) else "")
        finish = "stop" if found and result.get("finished", True) else ("stop" if found else "length")
        prompt_tokens = int(result.get("prompt_tokens", 0))
        completion_tokens = int(result.get("generation_tokens", 0))
        LOG.info("chat %.0f ms prompt=%d completion=%d json=%s", (time.perf_counter() - started) * 1000,
                 prompt_tokens, completion_tokens, found)
        return {"id": "mlx-" + str(int(started * 1000)), "object": "chat.completion",
                "model": engine.model_id,
                "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens}}

    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OpenAI-shaped loopback vision server on mlx-vlm.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--revision", default=None, help="Hugging Face commit to pin")
    parser.add_argument("--image-tokens", choices=["default", "min"], default="default")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args(argv)
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    engine = MlxEngine(args.model, revision=args.revision, image_tokens=args.image_tokens)
    LOG.info("loaded %s (mlx-vlm %s) in %.1f s", engine.model_id, engine.runtime_version, engine.load_s)
    uvicorn.run(create_app(engine), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
