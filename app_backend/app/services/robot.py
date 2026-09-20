from datetime import datetime, timedelta
import httpx
from fastapi import HTTPException
from pymongo import ReturnDocument
from ..schemas.common import iso


class Robot:
    def __init__(self, context, settings, client):
        self.ctx = context
        self.settings = settings
        self.client = client

    async def get(self, request_id, app_user_id):
        user, _ = await self.ctx.household(app_user_id)
        request = await self.ctx.get('dispatches', request_id)
        if request['dog_user_id'] != user['dog_user_id'] or (request['kind'] == 'message' and request['app_user_id'] != app_user_id):
            raise HTTPException(404, 'Request not found')
        return {key: request[key] for key in ('id', 'kind', 'status', 'timestamp', 'attempts', 'error', 'day') if key in request}

    async def change_status(self, request, status, error=None, next_attempt_at=None):
        async def save(session):
            current = await self.ctx.get('dispatches', request['id'], session)
            # A callback can finish before the original POST returns.
            if current['status'] == 'completed':
                return None
            updates = {'status': status, 'error': error}
            if next_attempt_at:
                updates['next_attempt_at'] = next_attempt_at
            await self.ctx.db.dispatches.update_one({'id': request['id']}, {'$set': updates}, session=session)
            if request['kind'] == 'message':
                await self.ctx.db.messages.update_one({'id': request['conversation_id'], 'messages.id': request['message_id']},
                    {'$set': {'messages.$.status': status if status in ('accepted', 'failed') else 'queued'}}, session=session)
            return {'id': request['id'], 'status': status, 'error': error}
        record = await self.ctx.database.transaction(save)
        if record:
            await self.ctx.events.publish('request.updated', record, dog_user_id=request['dog_user_id'],
                                    app_user_id=request.get('app_user_id'))

    async def dispatch_one(self):
        if not self.settings.robot_dispatch_enabled:
            return False
        now = self.ctx.clock()
        # A lease allows restart recovery after dying during a POST. Robot deduplicates by ID.
        request = await self.ctx.db.dispatches.find_one_and_update(
            {'$or': [{'status': 'queued', 'next_attempt_at': {'$lte': iso(now)}},
                     {'status': 'sending', 'lease_until': {'$lt': iso(now)}}]},
            {'$set': {'status': 'sending', 'lease_until': iso(now + timedelta(seconds=45))}, '$inc': {'attempts': 1}},
            sort=[('timestamp', 1)], return_document=ReturnDocument.AFTER)
        if not request:
            return False
        if request['kind'] == 'reminder' and now - datetime.fromisoformat(request['payload']['scheduled_at'].replace('Z', '+00:00')) > timedelta(minutes=15):
            await self.change_status(request, 'missed' if request['attempts'] == 1 else 'failed',
                                     'Reminder expired before confirmed acceptance; not replayed')
            return True
        if request['attempts'] > 5:
            await self.change_status(request, 'failed', 'Retry limit reached; robot acceptance is unknown')
            return True
        path = '/api/message-requests' if request['kind'] == 'message' else '/api/reminder-requests'
        secret = self.settings.internal_secret.get_secret_value()
        if not secret:
            await self.change_status(request, 'failed', 'Robot callback secret is not configured')
            return True
        retry = False
        try:
            response = await self.client.post(self.settings.robot_backend_url.rstrip('/') + path,
                json=request['payload'], headers={'X-Internal-Secret': secret, 'Idempotency-Key': str(request['id'])},
                timeout=self.settings.http_timeout_seconds)
            if response.status_code == 202:
                try:
                    ack = response.json()
                    valid = isinstance(ack, dict) and type(ack.get('request_id')) is int and ack['request_id'] == request['id'] and ack.get('status') == 'accepted'
                except ValueError:
                    valid = False
                if valid:
                    await self.change_status(request, 'accepted')
                    return True
                error, retry = 'Robot returned an invalid acceptance receipt', True
            elif response.status_code >= 500:
                error, retry = 'Robot service unavailable', True
            else:
                error = f'Robot rejected the request (HTTP {response.status_code})'
        except httpx.HTTPError:
            error, retry = 'Robot connection failed; acceptance is unknown', True
        status = 'queued' if retry and request['attempts'] < 5 else 'failed'
        await self.change_status(request, status, error, iso(now + timedelta(seconds=min(60, 2 ** request['attempts']))))
        return True
