import base64
import hashlib
import json
from datetime import date
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from ..schemas.common import iso, utcnow


def clean(record):
    if record is None:
        return None
    return {key: value for key, value in record.items() if not key.startswith('_')}


def cursor_encode(values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def cursor_decode(value, size):
    try:
        result = json.loads(base64.urlsafe_b64decode(value))
        if not isinstance(result, list) or len(result) != size:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid cursor') from None


async def page(collection, query, limit=50, cursor=None, sort=None, session=None):
    sort = sort or [('id', 1)]
    query = dict(query)
    if cursor:
        values = cursor_decode(cursor, len(sort))
        clauses = []
        for i, (key, direction) in enumerate(sort):
            clause = {sort[j][0]: values[j] for j in range(i)}
            clause[key] = {'$gt' if direction == 1 else '$lt': values[i]}
            clauses.append(clause)
        query = {'$and': [query, {'$or': clauses}]}
    rows = await collection.find(query, {'_id': 0}, session=session).sort(sort).limit(limit + 1).to_list()
    items = [clean(row) for row in rows[:limit]]
    return {'items': items, 'next_cursor': cursor_encode([items[-1][key] for key, _ in sort]) if len(rows) > limit else None}


class Context:
    def __init__(self, database, events, clock=utcnow):
        self.database = database
        self.db = database.db
        self.events = events
        self.clock = clock

    async def get(self, collection, id, session=None):
        result = await self.db[collection].find_one({'id': id}, session=session)
        if result is None:
            raise HTTPException(404, f'{collection} record not found')
        return clean(result)

    async def household(self, app_user_id, session=None):
        user = await self.get('app_users', app_user_id, session)
        dog = await self.get('dog_users', user['dog_user_id'], session)
        return user, dog

    def day(self, dog, value=None):
        if value is not None:
            return value.isoformat() if isinstance(value, date) else date.fromisoformat(value).isoformat()
        return self.clock().astimezone(ZoneInfo(dog['timezone'])).date().isoformat()

    async def mutate(self, scope, key, body, callback):
        payload = body.model_dump(mode='json') if hasattr(body, 'model_dump') else body
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        receipt_id = hashlib.sha256((scope + ':' + key).encode()).hexdigest()

        async def transaction(session):
            old = await self.db.idempotency.find_one({'_id': receipt_id}, session=session)
            if old:
                if old['fingerprint'] != fingerprint:
                    raise HTTPException(409, 'Idempotency key was used with different content')
                return old['result'], True
            result = await callback(session)
            await self.db.idempotency.insert_one({'_id': receipt_id, 'fingerprint': fingerprint,
                                                  'result': result, 'timestamp': iso(self.clock())}, session=session)
            return result, False

        return await self.database.transaction(transaction)
