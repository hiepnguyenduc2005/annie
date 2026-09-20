from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / '.env', extra='ignore')
    mongodb_uri: SecretStr = SecretStr('')
    mongodb_db: str = Field(default='annie_v2', pattern=r'^[A-Za-z0-9_-]+$')
    robot_backend_url: str = 'http://127.0.0.1:8080'
    internal_secret: SecretStr = SecretStr('')
    reminder_scheduler_enabled: bool = False
    robot_dispatch_enabled: bool = False
    worker_enabled: bool = True
    mongo_timeout_ms: int = Field(default=3000, ge=100, le=30000)
    http_timeout_seconds: float = Field(default=10, ge=1, le=30)
