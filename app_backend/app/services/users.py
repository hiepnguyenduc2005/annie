from .common import clean, page
from ..schemas.common import iso


class Users:
    def __init__(self, context):
        self.ctx = context

    async def create(self, collection, body, key):
        async def save(session):
            if collection == 'app_users':
                await self.ctx.get('dog_users', body.dog_user_id, session)
            record = {'id': await self.ctx.database.next_id(session), 'timestamp': iso(self.ctx.clock()), **body.model_dump()}
            await self.ctx.db[collection].insert_one(dict(record), session=session)
            return record
        result, _ = await self.ctx.mutate('create:' + collection, key, body, save)
        return result

    async def list(self, collection, limit, cursor, dog_user_id=None):
        if dog_user_id is not None:
            await self.ctx.get('dog_users', dog_user_id)
        return await page(self.ctx.db[collection], {'dog_user_id': dog_user_id} if dog_user_id else {}, limit, cursor)
