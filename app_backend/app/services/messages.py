from bson import BSON
from fastapi import HTTPException
from .common import clean
from ..schemas.common import iso

MAX_CONVERSATION_BYTES = 8 * 1024 * 1024


class Messages:
    def __init__(self, context):
        self.ctx = context

    def capacity(self, conversation):
        if len(BSON.encode(conversation)) > MAX_CONVERSATION_BYTES:
            raise HTTPException(413, 'Daily conversation is full; history has been preserved')

    async def create(self, body, key):
        async def save(session):
            _, dog = await self.ctx.household(body.app_user_id, session)
            day = self.ctx.day(dog)
            conversation = await self.ctx.db.messages.find_one({'app_user_id': body.app_user_id, 'day': day}, session=session)
            if not conversation:
                conversation = {'id': await self.ctx.database.next_id(session), 'app_user_id': body.app_user_id,
                                'dog_user_id': dog['id'], 'day': day, 'messages': []}
            message_id = await self.ctx.database.next_id(session)
            request_id = await self.ctx.database.next_id(session)
            now = iso(self.ctx.clock())
            entry = {'id': message_id, 'role': 'app_user', 'text': body.text, 'timestamp': now,
                     'request_id': request_id, 'status': 'queued'}
            conversation['messages'].append(entry)
            self.capacity(conversation)
            await self.ctx.db.messages.replace_one({'id': conversation['id']}, conversation, upsert=True, session=session)
            payload = {'request_id': request_id, 'conversation_id': conversation['id'], 'message_id': message_id,
                       'app_user_id': body.app_user_id, 'dog_user_id': dog['id'], 'day': day, 'text': body.text}
            dispatch = {'id': request_id, 'kind': 'message', 'status': 'queued', 'attempts': 0,
                        'next_attempt_at': now, 'timestamp': now, 'payload': payload,
                        'app_user_id': body.app_user_id, 'dog_user_id': dog['id'], 'conversation_id': conversation['id'],
                        'message_id': message_id, 'day': day}
            await self.ctx.db.dispatches.insert_one(dispatch, session=session)
            return {'conversation_id': conversation['id'], 'message_id': message_id, 'request_id': request_id,
                    'day': day, 'status': 'queued', 'entry': entry, 'dog_user_id': dog['id']}
        result, replay = await self.ctx.mutate(f'message:{body.app_user_id}', key, body, save)
        if not replay:
            await self.ctx.events.publish('message.created', result['entry'], dog_user_id=result['dog_user_id'], app_user_id=body.app_user_id)
        return {k: v for k, v in result.items() if k not in ('entry', 'dog_user_id')}

    async def list(self, app_user_id, day, limit, cursor):
        from .common import cursor_decode, cursor_encode
        _, dog = await self.ctx.household(app_user_id)
        day = self.ctx.day(dog, day)
        conversation = await self.ctx.db.messages.find_one({'app_user_id': app_user_id, 'day': day}, {'_id': 0})
        if not conversation:
            return {'id': None, 'app_user_id': app_user_id, 'dog_user_id': dog['id'], 'day': day, 'messages': [], 'next_cursor': None}
        after = cursor_decode(cursor, 1)[0] if cursor else 0
        if not isinstance(after, int):
            raise HTTPException(422, 'Invalid cursor')
        rows = [item for item in conversation['messages'] if item['id'] > after]
        conversation['messages'] = rows[:limit]
        conversation['next_cursor'] = cursor_encode([rows[limit-1]['id']]) if len(rows) > limit else None
        return conversation

    async def reply(self, body, key):
        async def save(session):
            request = await self.ctx.get('dispatches', body.request_id, session)
            if request['kind'] != 'message' or request['message_id'] != body.reply_to:
                raise HTTPException(409, 'Reply does not match the original message')
            conversation = await self.ctx.get('messages', request['conversation_id'], session)
            entry = {'id': await self.ctx.database.next_id(session), 'role': body.role, 'text': body.text,
                     'timestamp': iso(body.timestamp), 'request_id': request['id'], 'reply_to': body.reply_to}
            conversation['messages'].append(entry)
            if body.final:
                for item in conversation['messages']:
                    if item['id'] == body.reply_to:
                        item['status'] = 'completed'
                await self.ctx.db.dispatches.update_one({'id': request['id']}, {'$set': {'status': 'completed'}}, session=session)
            self.capacity(conversation)
            await self.ctx.db.messages.replace_one({'id': conversation['id']}, conversation, session=session)
            return {'entry': entry, 'dog_user_id': request['dog_user_id'], 'app_user_id': request['app_user_id']}
        result, replay = await self.ctx.mutate('robot:reply', key, body, save)
        if not replay:
            await self.ctx.events.publish('message.created', result['entry'], dog_user_id=result['dog_user_id'], app_user_id=result['app_user_id'])
        return result['entry']
