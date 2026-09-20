"""Durable request correlation and idempotent companion callbacks."""
import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from ..schemas import RemoteRequest


class Companion:
    def __init__(self, settings, client, legacy):
        self.settings, self.client, self.legacy = settings, client, legacy
        settings.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(settings.outbox_path) + '.v2')
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, fingerprint TEXT, kind TEXT, metadata TEXT, session TEXT, callback TEXT, delivered INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, due REAL DEFAULT 0)')
        self.db.commit()
        self.lock = asyncio.Lock()
        # Voice sessions are in memory: never silently replay accepted work after restart.
        for rid, kind, metadata in self.db.execute('SELECT id, kind, metadata FROM requests WHERE callback IS NULL').fetchall():
            self.finish(rid, kind, json.loads(metadata), 'inconclusive', 'Robot restarted before the outcome was confirmed.')

    async def accept(self, body, kind, agent, hub):
        data = body.model_dump(mode='json')
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        async with self.lock:
            row = self.db.execute('SELECT fingerprint, kind FROM requests WHERE id=?', (body.request_id,)).fetchone()
            if row:
                if row != (fingerprint, kind):
                    raise HTTPException(409, 'Request ID already used with different content')
                return {'request_id': body.request_id, 'status': 'accepted'}
            if body.dog_user_id != self.settings.companion_dog_user_id:
                raise HTTPException(409, 'This audio phone serves another resident')
            instruction = body.text if kind == 'message' else 'Remind the resident: ' + body.description
            ack = await agent.request(RemoteRequest(request_id=f'companion-{body.request_id}', type=kind, request=instruction))
            metadata = {key: data[key] for key in ('message_id', 'reminder_id', 'occurrence_id') if key in data}
            with self.db:
                self.db.execute('INSERT INTO requests(id,fingerprint,kind,metadata,session) VALUES(?,?,?,?,?)', (body.request_id, fingerprint, kind, json.dumps(metadata), ack.session_id))
            hub.offer(ack.session_id)
            return {'request_id': body.request_id, 'status': 'accepted'}

    def finish(self, rid, kind, metadata, status, summary):
        body = {'request_id': rid, 'timestamp': datetime.now(timezone.utc).isoformat()}
        if kind == 'message':
            path = '/api/messages/replies'
            body.update(reply_to=metadata['message_id'], role='robot', text=f'{status.capitalize()}: {summary}', final=True)
        else:
            path = '/api/notes'
            body.update(occurrence_id=metadata['occurrence_id'], reminder_id=metadata['reminder_id'], description=summary,
                        outcome='completed' if status == 'completed' else 'unknown')
        with self.db:
            self.db.execute('UPDATE requests SET callback=? WHERE id=? AND callback IS NULL', (json.dumps({'path': path, 'body': body}), rid))

    async def enqueue(self, event):
        row = self.db.execute('SELECT id,kind,metadata FROM requests WHERE session=?', (event.session_id,)).fetchone()
        if row:
            self.finish(row[0], row[1], json.loads(row[2]), event.status, event.summary)
        else:
            await self.legacy.enqueue(event)

    async def deliver_pending(self):
        await self.legacy.deliver_pending()
        async with self.lock:
            for rid, payload, attempts in self.db.execute('SELECT id,callback,attempts FROM requests WHERE callback IS NOT NULL AND delivered=0 AND due<=? LIMIT 10', (time.time(),)).fetchall():
                record = json.loads(payload)
                try:
                    response = await self.client.post(self.settings.companion_backend_url + record['path'], json=record['body'], headers={'X-Internal-Secret': self.settings.internal_secret.get_secret_value(), 'Idempotency-Key': f'robot-v2-{rid}-final'}, timeout=self.settings.delivery_timeout_seconds)
                    response.raise_for_status()
                except httpx.HTTPError:
                    with self.db:
                        self.db.execute('UPDATE requests SET attempts=attempts+1,due=? WHERE id=?', (time.time()+min(300, 2**min(attempts+1,8)), rid))
                else:
                    with self.db:
                        self.db.execute('UPDATE requests SET delivered=1 WHERE id=?', (rid,))

    def close(self):
        self.db.close()
