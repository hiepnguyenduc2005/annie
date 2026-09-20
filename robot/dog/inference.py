"""The one place Annie's agent calls a language/vision model. Swap the provider here, nowhere else.

Team decision (whiteboard, 2026-09-20 ~02:00): test with Gemini now, swap the end model
later, and keep every inference call in one module so the swap is a config change. Every
caller builds an OpenAI-style message list (text, optional images) and calls
`Inference.chat(...)`; this module picks the endpoint from `ANNIE_LLM_PROVIDER`:

  local       Ollama / mlx / the GX10's vLLM: OpenAI-compatible server on loopback (default)
  openrouter  https://openrouter.ai/api/v1 with OPENROUTER_API_KEY (e.g. google/gemini-2.5-flash)
  gemini      Google's OpenAI-compatible endpoint with GEMINI_API_KEY (e.g. gemini-2.5-flash)
  openai      https://api.openai.com/v1 with OPENAI_API_KEY (e.g. gpt-4o-mini)

Privacy rule, enforced here: camera frames only leave the machine when
`ANNIE_ALLOW_CLOUD_VISION=1` is set explicitly. Text-only calls (mission planning,
replies) may use any provider. Failures return `{"ok": False, ...}`; callers always have
a deterministic fallback. Nothing here logs prompts, images or keys; the `stats` counters
(calls, failures, latency) are the only thing that comes out besides the answer.
"""
from __future__ import annotations

import base64
import os
import time
from urllib.parse import urlsplit

PROVIDERS = {
    "local": {"base_url": os.environ.get("ANNIE_LLM_BASE_URL", "http://127.0.0.1:11434/v1"), "key_env": None,
              "model": os.environ.get("ANNIE_LLM_MODEL", "qwen3-vl:2b-instruct")},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "model": os.environ.get("ANNIE_LLM_MODEL", "google/gemini-2.5-flash")},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
               "model": os.environ.get("ANNIE_LLM_MODEL", "gemini-2.5-flash")},
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
               "model": os.environ.get("ANNIE_LLM_MODEL", "gpt-4o-mini")},
}


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


class Inference:
    """One client, one provider, bounded calls. Construct once; share."""

    def __init__(self, provider=None, *, model=None, base_url=None, api_key=None, timeout_s=12.0, allow_cloud_vision=None,
                 post=None):
        self.provider = (provider or os.environ.get("ANNIE_LLM_PROVIDER") or "local").lower()
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown provider {self.provider!r}; choose from {sorted(PROVIDERS)}")
        cfg = PROVIDERS[self.provider]
        self.base_url = (base_url or cfg["base_url"]).rstrip("/")
        self.model = model or cfg["model"]
        self.api_key = api_key if api_key is not None else (os.environ.get(cfg["key_env"]) if cfg["key_env"] else None)
        self.timeout_s = timeout_s
        self.allow_cloud_vision = (os.environ.get("ANNIE_ALLOW_CLOUD_VISION") == "1") if allow_cloud_vision is None \
            else bool(allow_cloud_vision)
        self._post = post  # test seam: post(url, json, headers, timeout) -> dict
        self.stats = {"calls": 0, "failures": 0, "last_ms": None, "refused_vision": 0}

    @property
    def is_local(self) -> bool:
        return urlsplit(self.base_url).hostname in ("127.0.0.1", "localhost", "::1")

    def describe(self) -> str:
        return f"{self.provider}:{self.model} @ {self.base_url} ({'local' if self.is_local else 'cloud'}; " \
               f"vision {'allowed' if self.is_local or self.allow_cloud_vision else 'text-only'})"

    def chat(self, messages: list[dict], *, images: list[bytes] | None = None, max_tokens=120, temperature=0.2) -> dict:
        """Returns {"ok", "text", "latency_ms", "provider", "model"} and never raises."""
        images = images or []
        if images and not self.is_local and not self.allow_cloud_vision:
            self.stats["refused_vision"] += 1
            return {"ok": False, "text": "", "error": "camera frames stay local (set ANNIE_ALLOW_CLOUD_VISION=1 to override)",
                    "provider": self.provider, "model": self.model}
        if not self.is_local and not self.api_key:
            return {"ok": False, "text": "", "error": f"no API key for provider {self.provider}", "provider": self.provider,
                    "model": self.model}
        if images:
            messages = [dict(m) for m in messages]
            last = messages[-1]
            content = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": str(last["content"])}]
            last["content"] = [image_part(j) for j in images] + content
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": False}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        t0 = time.perf_counter()
        self.stats["calls"] += 1
        try:
            if self._post is not None:
                data = self._post(self.base_url + "/chat/completions", body, headers, self.timeout_s)
            else:
                import httpx
                with httpx.Client(timeout=self.timeout_s, trust_env=False) as client:
                    r = client.post(self.base_url + "/chat/completions", json=body, headers=headers)
                    r.raise_for_status()
                    data = r.json()
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:
            self.stats["failures"] += 1
            self.stats["last_ms"] = round((time.perf_counter() - t0) * 1000)
            return {"ok": False, "text": "", "error": type(exc).__name__, "provider": self.provider, "model": self.model,
                    "latency_ms": self.stats["last_ms"]}
        self.stats["last_ms"] = round((time.perf_counter() - t0) * 1000)
        return {"ok": True, "text": text if isinstance(text, str) else str(text), "latency_ms": self.stats["last_ms"],
                "provider": self.provider, "model": self.model}


_SHARED: Inference | None = None


def shared() -> Inference:
    """Process-wide client from the environment (the patrol brain, errand and skills all use this)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = Inference()
    return _SHARED
