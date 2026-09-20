"""Qwen handles text reasoning; Deepgram handles speech.
Source checked against vLLM-Omni 573ec4cdace9dceafff8ee9f6aa11b207a832938.
See README for source references; live GB10 inference remains a deployment check.
"""

import base64
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import Settings

T = TypeVar("T", bound=BaseModel)


class QwenError(Exception):
    """Safe error: never contains a provider body or user input."""


class ModelClient(Protocol):
    async def reply(self, messages: list[dict]) -> str: ...
    async def structured(self, messages: list[dict], schema: type[T]) -> T: ...


def media_part(data: bytes, mime: str, kind: str) -> dict:
    key = f"{kind}_url"
    return {
        "type": key,
        key: {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"},
    }


class QwenClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def _request(self, messages: list[dict], *, max_tokens: int) -> str:
        try:
            response = await self.client.post(
                self.settings.qwen_base_url + "/v1/chat/completions",
                json={
                    "model": self.settings.qwen_model,
                    "messages": messages,
                    "modalities": ["text"],
                    "max_tokens": max_tokens,
                    "temperature": 0.4 if max_tokens == 500 else 0.0,
                    "stream": False,
                },
                timeout=self.settings.qwen_request_timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise QwenError("Qwen is unavailable or rejected the request") from None
        try:
            body = response.json()
            texts = []
            for choice in body["choices"]:
                message = choice["message"]
                if message.get("content"):
                    if not isinstance(message["content"], str):
                        raise ValueError
                    texts.append(message["content"])
            text = "\n".join(texts).strip()
            if not text or len(text) > 16000:
                raise ValueError
            return text
        except (ValueError, KeyError, TypeError, AttributeError):
            raise QwenError("Qwen returned an unexpected response") from None

    async def reply(self, messages: list[dict]) -> str:
        return await self._request(messages, max_tokens=500)

    async def structured(self, messages: list[dict], schema: type[T]) -> T:
        text = await self._request(messages, max_tokens=1600)
        try:
            # No assumed JSON-schema extension or extraction from mixed prose.
            return schema.model_validate_json(text)
        except ValidationError:
            raise QwenError("Qwen returned invalid internal analysis") from None
