import asyncio
from datetime import timedelta
import httpx
import pytest
from app.main import create_app
from app.seed import seed
from app.schemas.common import iso

pytestmark = pytest.mark.asyncio


def headers(key, robot=False):
    return {'Idempotency-Key': key, **({'X-Internal-Secret': 'test-secret'} if robot else {})}


async def test_seed_shared_reminders_and_new_member_addition(running):
    app, client = running
    assert (await client.get('/health')).json() == {'status': 'ok'}
    assert (await client.get('/ready')).status_code == 200
    zach = (await client.get('/api/reminders', params={'app_user_id': 2})).json()
    ellis = (await client.get('/api/reminders', params={'app_user_id': 3})).json()
    assert zach == ellis
    assert [r['daily_time'] for r in zach['items']] == ['08:00', '09:30', '12:30', '15:00', '18:00']
    assert [r['done'] for r in zach['items']] == [True, True, False, False, False]
    events = app.state.services.context.events
    subscribers = [events.subscribe({'id': id, 'dog_user_id': 1}) for id in (2, 3)]
    body = {'app_user_id': 2, 'daily_time': '16:45', 'description': 'Drink water'}
    response = await client.post('/api/reminders', json=body, headers=headers('water'))
    assert response.status_code == 201, response.text
    id = response.json()['id']
    assert type(id) is int
    assert all(s.queue.get_nowait()['data']['id'] == id for s in subscribers)
    assert id in [r['id'] for r in (await client.get('/api/reminders?app_user_id=3')).json()['items']]
    again = await client.post('/api/reminders', json=body, headers=headers('water'))
    assert again.json() == response.json()
    assert all(s.queue.empty() for s in subscribers)
    body['description'] = 'Different'
    assert (await client.post('/api/reminders', json=body, headers=headers('water'))).status_code == 409


async def test_restart_preserves_changes_and_daily_done_resets(running, clock):
    app, client = running
    await app.state.database.db.reminders.update_one({'id': 4}, {'$set': {'description': 'Changed'}})
    clock.value += timedelta(days=1)
    await seed(app.state.database, clock)
    rows = (await client.get('/api/reminders?app_user_id=2')).json()['items']
    assert rows[0]['description'] == 'Changed'
    assert all(not item['done'] for item in rows)
    assert await app.state.database.db.notes.count_documents({}) == 2


async def test_messages_concurrent_daily_unique_and_after_midnight_reply(running, clock):
    app, client = running
    async def post(index):
        return await client.post('/api/messages', json={'app_user_id': 2, 'text': f'Message {index}'}, headers=headers(f'm{index}'))
    responses = await asyncio.gather(*(post(i) for i in range(6)))
    assert all(r.status_code == 202 for r in responses), [r.text for r in responses]
    first = responses[0].json()
    assert await app.state.database.db.messages.count_documents({'app_user_id': 2}) == 1
    assert len((await client.get('/api/messages?app_user_id=2')).json()['messages']) == 6
    assert (await client.get('/api/messages?app_user_id=3')).json()['messages'] == []
    assert (await client.get(f'/api/requests/{first["request_id"]}?app_user_id=3')).status_code == 404
    assert (await post(0)).json() == first
    assert await app.state.database.db.dispatches.count_documents({'kind': 'message'}) == 6
    clock.value += timedelta(days=1)
    reply = {'request_id': first['request_id'], 'reply_to': first['message_id'], 'role': 'resident',
             'text': 'Hello', 'timestamp': iso(clock()), 'final': True}
    response = await client.post('/api/messages/replies', json=reply, headers=headers('reply', True))
    assert response.status_code == 201, response.text
    assert (await client.post('/api/messages/replies', json=reply, headers=headers('reply', True))).json() == response.json()
    assert (await client.get('/api/messages?app_user_id=2')).json()['messages'] == []
    old = (await client.get('/api/messages?app_user_id=2&day=2026-09-20')).json()
    assert len(old['messages']) == 7
    assert old['messages'][-1]['text'] == 'Hello'
    assert (await client.get(f'/api/requests/{first["request_id"]}?app_user_id=2')).json()['status'] == 'completed'


async def test_notifications_auth_history_and_scoping(running, clock):
    app, client = running
    body = {'dog_user_id': 1, 'timestamp': iso(clock()), 'description': 'Help requested', 'is_emergency': True}
    assert (await client.post('/api/notifications', json=body, headers=headers('n'))).status_code == 401
    result = await client.post('/api/notifications', json=body, headers=headers('n', True))
    assert result.status_code == 201, result.text
    assert (await client.post('/api/notifications', json=body, headers=headers('n', True))).json() == result.json()
    history = (await client.get('/api/history?app_user_id=3&limit=1')).json()
    assert history['items'][0]['kind'] == 'notification'
    assert history['next_cursor']
    next_page = (await client.get('/api/history', params={'app_user_id': 3, 'limit': 1, 'cursor': history['next_cursor']})).json()
    assert next_page['items'][0]['id'] != result.json()['id']
    other_dog = (await client.post('/api/dog-users', json={'name': 'Other'}, headers=headers('dog'))).json()
    other_user = (await client.post('/api/app-users', json={'name': 'Other family', 'dog_user_id': other_dog['id']}, headers=headers('user'))).json()
    assert (await client.get('/api/history', params={'app_user_id': other_user['id']})).json()['items'] == []
    assert (await client.get('/api/notes', params={'app_user_id': other_user['id'], 'reminder_id': 4})).status_code == 404


async def test_boundary_validation_and_pagination(running):
    app, client = running
    assert (await client.get('/api/reminders?app_user_id=999')).status_code == 404
    assert (await client.get('/api/reminders?app_user_id=2&day=bad')).status_code == 422
    assert (await client.get('/api/reminders?app_user_id=2&limit=1000')).status_code == 422
    assert (await client.get('/api/reminders?app_user_id=2&cursor=%%%')).status_code == 422
    assert (await client.get('/health', headers={'host': 'evil.example'})).status_code == 400
    assert (await client.get('/health', headers={'origin': 'http://evil.example'})).status_code == 403
    response = await client.post('/api/reminders', json={'app_user_id': 2, 'daily_time': '25:00', 'description': 'private text'}, headers=headers('bad'))
    assert response.status_code == 422 and 'private text' not in response.text
    assert (await client.post('/api/messages', json={'app_user_id': '2', 'text': 'hi'}, headers=headers('str'))).status_code == 422
    assert (await client.post('/api/messages', content='x' * 70000)).status_code == 413
    ids, cursor = [], None
    while True:
        params = {'app_user_id': 2, 'limit': 2}
        if cursor:
            params['cursor'] = cursor
        page = (await client.get('/api/reminders', params=params)).json()
        ids.extend(r['id'] for r in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert ids == [4, 5, 6, 7, 8]


async def test_failed_mutation_does_not_consume_retry_key(running):
    app, client = running
    body = {'name': 'Family', 'dog_user_id': 900}
    assert (await client.post('/api/app-users', json=body, headers=headers('unknown'))).status_code == 404
    assert await app.state.database.db.idempotency.count_documents({}) == 0


async def test_conversation_limit_rolls_back_dispatch(running, monkeypatch):
    import app.services.messages as module
    app, client = running
    monkeypatch.setattr(module, 'MAX_CONVERSATION_BYTES', 100)
    response = await client.post('/api/messages', json={'app_user_id': 2, 'text': 'hello'}, headers=headers('limit'))
    assert response.status_code == 413
    assert await app.state.database.db.messages.count_documents({}) == 0
    assert await app.state.database.db.dispatches.count_documents({'kind': 'message'}) == 0


async def test_real_database_reopen(settings, clock):
    app = create_app(settings, clock=clock)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            record = (await client.post('/api/reminders', json={'app_user_id': 3, 'daily_time': '16:30', 'description': 'Persistent'}, headers=headers('persist'))).json()
    reopened = create_app(settings, clock=clock)
    async with reopened.router.lifespan_context(reopened):
        try:
            rows = await reopened.state.database.db.reminders.find({'id': record['id']}).to_list()
            assert len(rows) == 1 and rows[0]['description'] == 'Persistent'
            assert await reopened.state.database.db.reminders.count_documents({}) == 6
        finally:
            await reopened.state.database.client.drop_database(settings.mongodb_db)


async def test_database_failure_returns_503_without_fake_write(running, monkeypatch):
    from pymongo.errors import AutoReconnect
    app, client = running
    async def unavailable(callback):
        raise AutoReconnect('sensitive provider detail')
    monkeypatch.setattr(app.state.database, 'transaction', unavailable)
    result = await client.post('/api/reminders', json={'app_user_id': 2, 'daily_time': '17:00', 'description': 'Test'}, headers=headers('outage'))
    assert result.status_code == 503
    assert 'sensitive provider detail' not in result.text
    assert await app.state.database.db.reminders.count_documents({}) == 5


async def test_duplicate_posts_racing_have_one_effect(running):
    app, client = running
    async def post():
        return await client.post('/api/messages', json={'app_user_id': 2, 'text': 'One request'}, headers=headers('same-key'))
    responses = await asyncio.gather(*(post() for _ in range(8)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()['message_id'] for response in responses}) == 1
    assert await app.state.database.db.dispatches.count_documents({'kind': 'message'}) == 1


async def test_reminder_completion_waits_until_due_and_keeps_latest_note(running, clock):
    app, client = running
    # Seed reports exist, but at 07:59 New York neither morning reminder is due.
    clock.value = clock.value.replace(hour=11, minute=59)
    async def rows():
        return (await client.get('/api/reminders?app_user_id=2')).json()['items']
    before = await rows()
    assert not before[0]['done']
    assert before[0]['latest_note']['outcome'] == 'completed'
    clock.value = clock.value.replace(hour=12, minute=0)
    at_time = await rows()
    assert at_time[0]['done']
    assert not at_time[1]['done']
    clock.value += timedelta(days=1)
    tomorrow = await rows()
    assert not tomorrow[0]['done']
    assert tomorrow[0]['latest_note'] == before[0]['latest_note']
    # A newer negative report overrides a completed report for the same day.
    clock.value -= timedelta(days=1)
    clock.value = clock.value.replace(hour=15)
    note = dict(before[0]['latest_note'])
    note.update(id=999, timestamp=iso(clock.value + timedelta(minutes=1)), outcome='not_completed')
    await app.state.database.db.notes.insert_one(note)
    clock.value += timedelta(minutes=2)
    updated = await rows()
    assert not updated[0]['done']
    assert updated[0]['latest_note']['id'] == 999
