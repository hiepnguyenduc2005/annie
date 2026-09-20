"""Persistent records for the household: profiles, messages, reminders,
reminder history, and emergencies.

Backed by MongoDB when `MONGODB_URI` is set and reachable. If it is not, the
store falls back to an in-process dictionary with the same interface and says
so in `backend`. The fallback exists so a database that is down cannot take
the family app down with it; it is not a silent substitute, and anything
written to it is lost on restart.

Collections mirror the agreed schema:

  app_users       id (int), name, dog_user_id
  dog_users       id, name
  messages        id, date, dog_user_id, app_user_id, texts[]
  reminders       id, dog_user_id, hour, item
  history_records id, reminder_id, timedate, description
  emergencies     id, dog_user_id, timestamp, description

App users are many-to-one onto dog users: several family members can share one
resident.
"""
import os
import time
from uuid import uuid4

COLLECTIONS = ('app_users', 'dog_users', 'messages', 'reminders',
               'history_records', 'emergencies')


def now_ms():
    return int(time.time() * 1000)


def new_id():
    return uuid4().hex[:12]


class StoreError(Exception):
    """A referenced record does not exist."""


class SchemaStore:
    def __init__(self, uri=None, db_name=None, connect_timeout_ms=1500):
        self.uri = uri if uri is not None else os.getenv('MONGODB_URI', '')
        self.db_name = db_name or os.getenv('MONGODB_DB', 'annie')
        self.connect_timeout_ms = connect_timeout_ms
        self.client = None
        self.db = None
        self.backend = 'memory'
        self.reason = 'MONGODB_URI is not set'
        self._memory = {name: [] for name in COLLECTIONS}

    async def connect(self):
        """Try Mongo once at startup; fall back to memory with a reason."""
        if not self.uri:
            return self.backend
        try:
            from pymongo import AsyncMongoClient

            client = AsyncMongoClient(self.uri, serverSelectionTimeoutMS=self.connect_timeout_ms)
            await client.admin.command('ping')
        except Exception as exc:
            self.reason = f'{type(exc).__name__}: {exc}'
            return self.backend
        self.client = client
        self.db = client[self.db_name]
        self.backend = 'mongodb'
        self.reason = ''
        await self.db.app_users.create_index('dog_user_id')
        await self.db.messages.create_index('dog_user_id')
        await self.db.reminders.create_index('dog_user_id')
        await self.db.history_records.create_index('reminder_id')
        await self.db.emergencies.create_index('dog_user_id')
        return self.backend

    async def close(self):
        if self.client is not None:
            await self.client.close()

    def status(self):
        return {'backend': self.backend, 'database': self.db_name if self.backend == 'mongodb' else None,
                'reason': self.reason or None}

    # ---- storage primitives -------------------------------------------------

    async def _insert(self, collection, document):
        if self.backend == 'mongodb':
            await self.db[collection].insert_one(dict(document))
        else:
            self._memory[collection].append(dict(document))
        return document

    async def _find(self, collection, query=None):
        query = query or {}
        if self.backend == 'mongodb':
            cursor = self.db[collection].find(query, {'_id': 0})
            return [document async for document in cursor]
        return [dict(item) for item in self._memory[collection]
                if all(item.get(key) == value for key, value in query.items())]

    async def _find_one(self, collection, query):
        found = await self._find(collection, query)
        return found[0] if found else None

    async def _update(self, collection, query, changes=None, push=None):
        if self.backend == 'mongodb':
            operation = {}
            if changes:
                operation['$set'] = changes
            if push:
                operation['$push'] = push
            result = await self.db[collection].update_one(query, operation)
            return result.matched_count > 0
        for item in self._memory[collection]:
            if all(item.get(key) == value for key, value in query.items()):
                item.update(changes or {})
                for field, value in (push or {}).items():
                    item.setdefault(field, []).append(value)
                return True
        return False

    # ---- dog users ----------------------------------------------------------

    async def create_dog_user(self, name, dog_user_id=None):
        record = {'id': dog_user_id or new_id(), 'name': name}
        if await self._find_one('dog_users', {'id': record['id']}):
            raise StoreError(f'dog user {record["id"]} already exists')
        return await self._insert('dog_users', record)

    async def dog_users(self):
        return await self._find('dog_users')

    async def dog_user(self, dog_user_id):
        return await self._find_one('dog_users', {'id': dog_user_id})

    # ---- app users ----------------------------------------------------------

    async def create_app_user(self, name, dog_user_id):
        if not await self.dog_user(dog_user_id):
            raise StoreError(f'unknown dog user {dog_user_id}')
        existing = await self._find('app_users')
        next_id = max((item['id'] for item in existing), default=0) + 1
        return await self._insert('app_users', {'id': next_id, 'name': name, 'dog_user_id': dog_user_id})

    async def app_users(self, dog_user_id=None):
        return await self._find('app_users', {'dog_user_id': dog_user_id} if dog_user_id else None)

    async def app_user(self, app_user_id):
        return await self._find_one('app_users', {'id': app_user_id})

    # ---- messages -----------------------------------------------------------

    async def create_message(self, dog_user_id, app_user_id, text):
        """Start a message. `texts` accumulates: the family's text first, then
        whatever the robot reports back about delivering it."""
        if not await self.dog_user(dog_user_id):
            raise StoreError(f'unknown dog user {dog_user_id}')
        if app_user_id is not None and not await self.app_user(app_user_id):
            raise StoreError(f'unknown app user {app_user_id}')
        record = {'id': new_id(), 'date': now_ms(), 'dog_user_id': dog_user_id,
                  'app_user_id': app_user_id,
                  'texts': [{'from': 'app', 'text': text, 'at': now_ms()}]}
        return await self._insert('messages', record)

    async def append_message_text(self, message_id, text, source='robot'):
        """The yellow path: robot_backend's summary of the dog's action or the
        resident's audio response."""
        entry = {'from': source, 'text': text, 'at': now_ms()}
        if not await self._update('messages', {'id': message_id}, push={'texts': entry}):
            raise StoreError(f'unknown message {message_id}')
        return entry

    async def messages(self, dog_user_id=None):
        found = await self._find('messages', {'dog_user_id': dog_user_id} if dog_user_id else None)
        return sorted(found, key=lambda item: item['date'])

    async def message(self, message_id):
        return await self._find_one('messages', {'id': message_id})

    # ---- reminders ----------------------------------------------------------

    async def create_reminder(self, dog_user_id, hour, item):
        if not await self.dog_user(dog_user_id):
            raise StoreError(f'unknown dog user {dog_user_id}')
        return await self._insert('reminders', {'id': new_id(), 'dog_user_id': dog_user_id,
                                                'hour': hour, 'item': item})

    async def reminders(self, dog_user_id=None):
        found = await self._find('reminders', {'dog_user_id': dog_user_id} if dog_user_id else None)
        return sorted(found, key=lambda item: item['hour'])

    async def reminder(self, reminder_id):
        return await self._find_one('reminders', {'id': reminder_id})

    # ---- history records ----------------------------------------------------

    async def create_history_record(self, reminder_id, description, timedate=None):
        """The red path: what actually happened for a reminder, e.g. "grandma
        took her pills"."""
        if not await self.reminder(reminder_id):
            raise StoreError(f'unknown reminder {reminder_id}')
        return await self._insert('history_records', {
            'id': new_id(), 'reminder_id': reminder_id,
            'timedate': timedate if timedate is not None else now_ms(),
            'description': description})

    async def history_records(self, reminder_id=None):
        found = await self._find('history_records', {'reminder_id': reminder_id} if reminder_id else None)
        return sorted(found, key=lambda item: item['timedate'])

    # ---- emergencies --------------------------------------------------------

    async def create_emergency(self, dog_user_id, description, timestamp=None):
        """The cyan path: emergency text received from robot_backend."""
        if not await self.dog_user(dog_user_id):
            raise StoreError(f'unknown dog user {dog_user_id}')
        return await self._insert('emergencies', {
            'id': new_id(), 'dog_user_id': dog_user_id,
            'timestamp': timestamp if timestamp is not None else now_ms(),
            'description': description})

    async def emergencies(self, dog_user_id=None):
        found = await self._find('emergencies', {'dog_user_id': dog_user_id} if dog_user_id else None)
        return sorted(found, key=lambda item: item['timestamp'], reverse=True)
