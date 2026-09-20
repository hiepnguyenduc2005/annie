"""Qwen handles text reasoning; Deepgram handles speech.
Source checked against vLLM-Omni 573ec4cdace9dceafff8ee9f6aa11b207a832938.
See README for source references; live GB10 inference remains a deployment check.
"""

import base64
import json
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
        # End with an explicit analysis request rather than an assistant reply
        # that the chat model may simply continue. No server-specific extensions.
        instruction = (
            "Analyze the preceding session data. Return only one JSON object "
            "matching this schema. Do not continue the conversation or invent "
            "evidence. Use inconclusive/active and false when unsupported. Schema: "
            + json.dumps(schema.model_json_schema())
        )
        text = await self._request(
            [*messages, {"role": "user", "content": instruction}], max_tokens=1600
        )
        # Accept a single enclosing JSON code fence, but never fish an object
        # out of arbitrary prose or fill in missing evidence-bearing fields.
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip() in {"```json", "```"} and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            # Error codes only: locations, input values and messages can contain
            # private model output. A response reached us before this validation.
            codes = sorted({error["type"] for error in exc.errors(include_input=False, include_context=False)})
            raise QwenError(
                "Qwen responded, but internal analysis failed validation: "
                + ", ".join(codes)
            ) from None
