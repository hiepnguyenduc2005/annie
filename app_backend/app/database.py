import asyncio
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from .schemas.common import MAX_ID

COLLECTIONS = ('dog_users', 'app_users', 'reminders', 'notes', 'messages', 'notifications', 'dispatches')


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.db = None

    async def connect(self):
        uri = self.settings.mongodb_uri.get_secret_value()
        if not uri:
            raise RuntimeError('Set MONGODB_URI in app_backend/.env before starting.')
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=self.settings.mongo_timeout_ms,
                                       timeoutMS=10000, tz_aware=True)
        self.db = self.client[self.settings.mongodb_db]
        try:
            hello = await self.client.admin.command('hello')
            if not hello.get('setName') and hello.get('msg') != 'isdbgrid':
                raise RuntimeError('MongoDB must be Atlas or a replica set for atomic writes.')
            await self.indexes()
        except RuntimeError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise RuntimeError('MongoDB connection failed. Check URI, network access, and database permissions.') from None

    async def indexes(self):
        for name in COLLECTIONS:
            await self.db[name].create_index('id', unique=True)
        await self.db.app_users.create_index('dog_user_id')
        await self.db.reminders.create_index([('dog_user_id', 1), ('daily_time', 1)])
        await self.db.messages.create_index([('app_user_id', 1), ('day', 1)], unique=True)
        for name in ('notes', 'notifications'):
            await self.db[name].create_index([('dog_user_id', 1), ('timestamp', -1), ('id', -1)])
        await self.db.notes.create_index([('reminder_id', 1), ('day', 1), ('timestamp', -1), ('id', -1)])
        await self.db.dispatches.create_index([('reminder_id', 1), ('day', 1)], unique=True,
                                             partialFilterExpression={'kind': 'reminder'})
        await self.db.dispatches.create_index([('status', 1), ('next_attempt_at', 1)])
        await self.db.counters.update_one({'_id': 'ids'}, {'$setOnInsert': {'value': 20}}, upsert=True)
        # Counter metadata survives restarts. Recover above existing records if manually removed.
        maximum = 20
        for name in COLLECTIONS:
            record = await self.db[name].find_one({}, sort=[('id', -1)])
            maximum = max(maximum, record['id'] if record else 0)
        record = await self.db.messages.aggregate([{'$unwind': '$messages'}, {'$group': {'_id': None, 'value': {'$max': '$messages.id'}}}])
        async for item in record:
            maximum = max(maximum, item['value'])
        await self.db.counters.update_one({'_id': 'ids'}, {'$max': {'value': maximum}})

    async def next_id(self, session):
        record = await self.db.counters.find_one_and_update(
            {'_id': 'ids', 'value': {'$lt': MAX_ID}}, {'$inc': {'value': 1}},
            return_document=ReturnDocument.AFTER, session=session)
        if not record:
            raise RuntimeError('Numeric ID space exhausted')
        return record['value']

    async def transaction(self, callback):
        async with asyncio.timeout(20):
            return await self._transaction(callback)

    async def _transaction(self, callback):
        # Unique-index races may surface as DuplicateKey rather than a transaction retry label.
        for attempt in range(5):
            try:
                async with self.client.start_session() as session:
                    return await session.with_transaction(callback, read_concern=ReadConcern('snapshot'),
                                                          write_concern=WriteConcern('majority'))
            except DuplicateKeyError:
                if attempt == 4:
                    raise
                await asyncio.sleep(.01 * (attempt + 1))

    async def close(self):
        if self.client:
            await self.client.close()
