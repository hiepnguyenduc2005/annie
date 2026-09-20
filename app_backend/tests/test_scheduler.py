from datetime import datetime, timedelta, timezone
import httpx
import pytest
from app.scheduler import due_time
from app.schemas.common import iso
from app.services.robot import Robot
from app.schemas.reminders import NewNote


def test_dst_gap_and_fold():
    assert due_time('2026-03-08', '02:30', 'America/New_York') == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    assert due_time('2026-11-01', '01:30', 'America/New_York') == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_due_once_notes_and_missed_occurrences(running, clock):
    app, _ = running
    svc = app.state.services
    svc.scheduler.settings.reminder_scheduler_enabled = True
    await svc.scheduler.schedule()
    assert await svc.context.db.dispatches.count_documents({'status': 'queued'}) == 0
    clock.value = datetime(2026, 9, 21, 13, 35, tzinfo=timezone.utc)  # 09:35 New York
    await svc.scheduler.schedule()
    await svc.scheduler.schedule()
    due = await svc.context.db.dispatches.find_one({'kind': 'reminder', 'reminder_id': 5, 'day': '2026-09-21'})
    assert due['status'] == 'queued'
    assert due['payload']['scheduled_at'] == '2026-09-21T13:30:00.000Z'
    assert await svc.context.db.dispatches.count_documents({'reminder_id': 5, 'day': '2026-09-21'}) == 1
    missed = await svc.context.db.dispatches.find_one({'reminder_id': 4, 'day': '2026-09-21'})
    assert missed['status'] == 'missed'
    body = NewNote(request_id=due['id'], occurrence_id=due['id'], reminder_id=5,
                   timestamp=clock(), description='Completed', outcome='completed')
    result = await svc.reminders.add_note(body, 'note')
    assert (await svc.reminders.add_note(body, 'note')) == result
    rows = await svc.reminders.list(2, None, 50, None)
    assert next(r for r in rows['items'] if r['id'] == 5)['done']
    clock.value += timedelta(days=1)
    rows = await svc.reminders.list(2, None, 50, None)
    assert not next(r for r in rows['items'] if r['id'] == 5)['done']


@pytest.mark.asyncio
async def test_dispatch_retry_stable_id_and_accepted_not_retried(running, clock):
    from app.schemas.messages import NewMessage
    app, _ = running
    svc = app.state.services
    ack = await svc.messages.create(NewMessage(app_user_id=2, text='Hello'), 'dispatch')
    svc.robot.settings.robot_dispatch_enabled = True
    seen = []
    async def handler(request):
        import json
        payload = json.loads(request.content)
        seen.append(payload['request_id'])
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(202, json={'request_id': payload['request_id'], 'status': 'accepted'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        robot = Robot(svc.context, svc.robot.settings, client)
        await robot.dispatch_one()
        assert (await robot.get(ack['request_id'], 2))['status'] == 'queued'
        clock.value += timedelta(seconds=5)
        await robot.dispatch_one()
        assert (await robot.get(ack['request_id'], 2))['status'] == 'accepted'
        assert not await robot.dispatch_one()
    assert seen == [ack['request_id'], ack['request_id']]


@pytest.mark.asyncio
async def test_callback_before_ack_does_not_regress_completed(running, clock):
    from app.schemas.messages import NewMessage, MessageReply
    app, _ = running
    svc = app.state.services
    ack = await svc.messages.create(NewMessage(app_user_id=2, text='Hi'), 'race')
    svc.robot.settings.robot_dispatch_enabled = True
    async def handler(request):
        await svc.messages.reply(MessageReply(request_id=ack['request_id'], reply_to=ack['message_id'], role='robot',
                                             text='Finished', timestamp=clock(), final=True), 'early')
        return httpx.Response(202, json={'request_id': ack['request_id'], 'status': 'accepted'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        robot = Robot(svc.context, svc.robot.settings, client)
        await robot.dispatch_one()
        assert (await robot.get(ack['request_id'], 2))['status'] == 'completed'


@pytest.mark.asyncio
async def test_queued_reminder_expires_without_robot_call(running, clock):
    app, _ = running
    svc = app.state.services
    svc.scheduler.settings.reminder_scheduler_enabled = True
    clock.value = datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)
    await svc.scheduler.schedule()
    clock.value += timedelta(hours=2)
    svc.robot.settings.robot_dispatch_enabled = True
    async def handler(request):
        pytest.fail('Expired reminder must not reach robot')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        robot = Robot(svc.context, svc.robot.settings, client)
        assert await robot.dispatch_one()
    request = await svc.context.db.dispatches.find_one({'reminder_id': 5, 'day': '2026-09-21'})
    assert request['status'] == 'missed'
