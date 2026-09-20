"""Durable summary-only outbox. No transcript, prompt or media is stored here."""

import logging
import sqlite3
import time
from typing import Protocol

import httpx

from ..config import Settings
from ..schemas import FinalEvent

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    pass


class FinalEventSink(Protocol):
    async def enqueue(self, event: FinalEvent) -> None: ...
    async def deliver_pending(self) -> None: ...


class DedicatedServer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        settings.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.outbox_path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS outbox (
            session_id TEXT PRIMARY KEY, payload TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            delivered_at REAL
        )""")
        self.db.commit()

    async def enqueue(self, event: FinalEvent) -> None:
        try:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox (session_id, payload) VALUES (?, ?)",
                    (event.session_id, event.model_dump_json()),
                )
        except sqlite3.Error:
            raise DeliveryError(
                "Could not queue the final result; retry finalization"
            ) from None

    async def deliver_pending(self) -> None:
        if not self.settings.dedicated_server_url:
            return
        rows = self.db.execute(
            "SELECT session_id, payload, attempts FROM outbox WHERE delivered_at IS NULL AND next_attempt <= ? LIMIT 10",
            (time.time(),),
        ).fetchall()
        for session_id, payload, attempts in rows:
            headers = {
                "Content-Type": "application/json",
                "Idempotency-Key": session_id,
            }
            key = self.settings.dedicated_server_api_key.get_secret_value()
            if key:
                headers["Authorization"] = "Bearer " + key
            try:
                response = await self.client.post(
                    self.settings.dedicated_server_url,
                    content=payload,
                    headers=headers,
                    timeout=self.settings.delivery_timeout_seconds,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                with self.db:
                    self.db.execute(
                        "UPDATE outbox SET attempts=?, next_attempt=? WHERE session_id=?",
                        (
                            attempts + 1,
                            time.time() + min(300, 2 ** min(attempts + 1, 8)),
                            session_id,
                        ),
                    )
                logger.warning("Final-result delivery failed; retry scheduled")
            else:
                with self.db:
                    self.db.execute(
                        "UPDATE outbox SET payload=NULL, delivered_at=? WHERE session_id=?",
                        (time.time(), session_id),
                    )

    def close(self) -> None:
        self.db.close()
