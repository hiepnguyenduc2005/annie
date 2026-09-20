from fastapi import HTTPException
from .common import page, cursor_decode, cursor_encode
from ..schemas.common import iso


class Notifications:
    def __init__(self, context):
        self.ctx = context

    async def create(self, body, key):
        async def save(session):
            await self.ctx.get('dog_users', body.dog_user_id, session)
            record = {**body.model_dump(mode='json'), 'timestamp': iso(body.timestamp),
                      'id': await self.ctx.database.next_id(session), 'source': 'robot'}
            await self.ctx.db.notifications.insert_one(dict(record), session=session)
            return record
        result, replay = await self.ctx.mutate('robot:notification', key, body, save)
        if not replay:
            await self.ctx.events.publish('notification.created', result, dog_user_id=result['dog_user_id'])
        return result

    async def list(self, app_user_id, is_emergency, limit, cursor):
        _, dog = await self.ctx.household(app_user_id)
        query = {'dog_user_id': dog['id']}
        if is_emergency is not None:
            query['is_emergency'] = is_emergency
        return await page(self.ctx.db.notifications, query, limit, cursor, [('timestamp', -1), ('id', -1)])

    async def history(self, app_user_id, limit, cursor):
        _, dog = await self.ctx.household(app_user_id)
        query = {'dog_user_id': dog['id']}
        if cursor:
            timestamp, id = cursor_decode(cursor, 2)
            if not isinstance(timestamp, str) or not isinstance(id, int):
                raise HTTPException(422, 'Invalid cursor')
            query['$or'] = [{'timestamp': {'$lt': timestamp}}, {'timestamp': timestamp, 'id': {'$lt': id}}]
        items = []
        for name, kind in [('notes', 'note'), ('notifications', 'notification')]:
            rows = await self.ctx.db[name].find(query, {'_id': 0}).sort([('timestamp', -1), ('id', -1)]).limit(limit + 1).to_list()
            for row in rows:
                items.append({**{key: row[key] for key in ('id', 'timestamp', 'dog_user_id', 'description', 'source', 'reminder_id', 'outcome') if key in row}, 'kind': kind, 'is_emergency': row.get('is_emergency', False)})
        items.sort(key=lambda row: (row['timestamp'], row['id']), reverse=True)
        selected = items[:limit]
        return {'items': selected, 'next_cursor': cursor_encode([selected[-1]['timestamp'], selected[-1]['id']]) if len(items) > limit else None}
