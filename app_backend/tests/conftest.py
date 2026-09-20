import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
import httpx
import pytest
import pytest_asyncio
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.main import create_app


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings():
    return Settings(_env_file=None, mongodb_uri='mongodb://127.0.0.1:27029/?replicaSet=annieTest',
                    mongodb_db='annie_test_' + uuid4().hex, internal_secret='test-secret', worker_enabled=False)


@pytest_asyncio.fixture
async def running(settings, clock):
    app = create_app(settings, clock=clock)
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://172.20.10.8:8000') as client:
                yield app, client
        finally:
            await app.state.database.client.drop_database(settings.mongodb_db)
