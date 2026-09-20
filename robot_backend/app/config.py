from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    internal_secret: SecretStr = SecretStr("")
    companion_backend_url: str = "http://127.0.0.1:8000"
    companion_dog_user_id: int = Field(1, gt=0)

    qwen_base_url: str = "http://100.112.123.40:8091"
    qwen_model: str = "Qwen/Qwen2.5-Omni-3B"
    qwen_request_timeout: float = Field(180, gt=0, le=600)
    deepgram_api_key: SecretStr = SecretStr("")
    deepgram_base_url: str = "https://api.deepgram.com"
    deepgram_stt_model: str = "nova-3"
    deepgram_tts_model: str = "aura-2-thalia-en"
    deepgram_language: str = "en"
    deepgram_request_timeout: float = Field(30, gt=0, le=180)
    audio_endpointing_ms: int = Field(700, ge=300, le=2000)
    audio_playback_grace_seconds: float = Field(10, ge=1, le=30)
    final_result_delivery_enabled: bool = False
    dedicated_server_url: str = ""
    dedicated_server_api_key: SecretStr = SecretStr("")
    phone_api_key: SecretStr = SecretStr("")
    server_api_key: SecretStr = SecretStr("")
    session_timeout_seconds: float = Field(900, gt=0)
    session_retention_seconds: float = Field(86400, gt=0)
    max_sessions: int = Field(256, ge=1, le=10000)
    history_turns: int = Field(6, ge=1, le=20)
    max_upload_bytes: int = Field(10 * 1024 * 1024, ge=1)
    outbox_path: Path = Path(".data/companion-outbox.sqlite3")
    delivery_timeout_seconds: float = Field(10, gt=0, le=60)
    maintenance_interval_seconds: float = Field(5, gt=0)

    @field_validator("qwen_base_url", "dedicated_server_url", "deepgram_base_url", "companion_backend_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
            ):
                raise ValueError("Use an HTTP(S) URL without credentials")
        return value.rstrip("/")
