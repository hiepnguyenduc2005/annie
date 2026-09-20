from zoneinfo import ZoneInfo
from fastapi import HTTPException
from .common import page
from ..schemas.common import iso


class Reminders:
    def __init__(self, context):
        self.ctx = context

    async def create(self, body, key):
        async def save(session):
            user, dog = await self.ctx.household(body.app_user_id, session)
            record = {'id': await self.ctx.database.next_id(session), 'dog_user_id': dog['id'],
                      'daily_time': body.daily_time, 'description': body.description,
                      'timestamp': iso(self.ctx.clock()), 'enabled': True}
            await self.ctx.db.reminders.insert_one(dict(record), session=session)
            return record
        result, replay = await self.ctx.mutate(f'reminder:{body.app_user_id}', key, body, save)
        if not replay:
            await self.ctx.events.publish('reminder.created', result, dog_user_id=result['dog_user_id'])
        return result

    async def list(self, app_user_id, day, limit, cursor):
        _, dog = await self.ctx.household(app_user_id)
        now = self.ctx.clock().astimezone(ZoneInfo(dog['timezone']))
        day = self.ctx.day(dog, day)
        result = await page(self.ctx.db.reminders, {'dog_user_id': dog['id']}, limit, cursor,
                            [('daily_time', 1), ('id', 1)])
        for record in result['items']:
            note = await self.ctx.db.notes.find_one({'reminder_id': record['id'], 'day': day},
                                                    {'_id': 0}, sort=[('timestamp', -1), ('id', -1)])
            latest = await self.ctx.db.notes.find_one({'reminder_id': record['id']}, {'_id': 0},
                                                      sort=[('timestamp', -1), ('id', -1)])
            due = (day, record['daily_time']) <= (now.date().isoformat(), now.strftime('%H:%M'))
            record['done'] = bool(due and note and note['outcome'] == 'completed')
            record['latest_note'] = latest
        result['day'] = day
        return result

    async def add_note(self, body, key):
        async def save(session):
            request = await self.ctx.get('dispatches', body.request_id, session)
            if request['kind'] != 'reminder' or request['id'] != body.occurrence_id or request['reminder_id'] != body.reminder_id:
                raise HTTPException(409, 'Reminder report does not match its request')
            if request['status'] in ('missed', 'seed'):
                raise HTTPException(409, 'This occurrence was not dispatched')
            await self.ctx.get('reminders', body.reminder_id, session)
            record = {**body.model_dump(mode='json'), 'timestamp': iso(body.timestamp),
                      'id': await self.ctx.database.next_id(session), 'dog_user_id': request['dog_user_id'],
                      'day': request['day'], 'source': 'robot'}
            await self.ctx.db.notes.insert_one(dict(record), session=session)
            await self.ctx.db.dispatches.update_one({'id': request['id']}, {'$set': {'status': 'completed'}}, session=session)
            return record
        result, replay = await self.ctx.mutate('robot:note', key, body, save)
        if not replay:
            await self.ctx.events.publish('note.created', result, dog_user_id=result['dog_user_id'])
        return result

    async def notes(self, app_user_id, reminder_id, limit, cursor):
        _, dog = await self.ctx.household(app_user_id)
        query = {'dog_user_id': dog['id']}
        if reminder_id:
            reminder = await self.ctx.get('reminders', reminder_id)
            if reminder['dog_user_id'] != dog['id']:
                raise HTTPException(404, 'Reminder not found in household')
            query['reminder_id'] = reminder_id
        return await page(self.ctx.db.notes, query, limit, cursor, [('timestamp', -1), ('id', -1)])
