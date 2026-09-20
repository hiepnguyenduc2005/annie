import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pymongo.errors import PyMongoError
from .schemas.common import iso

logger = logging.getLogger(__name__)


def due_time(day, daily_time, zone):
    """First occurrence in a fold; first valid minute after a spring-forward gap."""
    local = datetime.fromisoformat(f'{day}T{daily_time}').replace(tzinfo=ZoneInfo(zone), fold=0)
    for _ in range(181):
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(ZoneInfo(zone)).replace(tzinfo=None) == local.replace(tzinfo=None):
            return utc
        local += timedelta(minutes=1)
    raise ValueError('No valid local reminder time')


class Scheduler:
    def __init__(self, context, settings, robot):
        self.ctx, self.settings, self.robot = context, settings, robot

    async def schedule(self):
        if not self.settings.reminder_scheduler_enabled:
            return
        now = self.ctx.clock()
        async for dog in self.ctx.db.dog_users.find({}):
            day = now.astimezone(ZoneInfo(dog['timezone'])).date().isoformat()
            async for reminder in self.ctx.db.reminders.find({'dog_user_id': dog['id'], 'enabled': True}):
                scheduled = due_time(day, reminder['daily_time'], dog['timezone'])
                if scheduled > now or day < reminder.get('_schedule_from', day) or iso(scheduled) <= reminder['timestamp']:
                    continue
                async def create(session):
                    old = await self.ctx.db.dispatches.find_one({'kind': 'reminder', 'reminder_id': reminder['id'], 'day': day}, session=session)
                    if old:
                        return
                    id = await self.ctx.database.next_id(session)
                    payload = {'request_id': id, 'occurrence_id': id, 'dog_user_id': dog['id'], 'reminder_id': reminder['id'],
                               'day': day, 'daily_time': reminder['daily_time'], 'scheduled_at': iso(scheduled), 'description': reminder['description']}
                    record = {'id': id, 'kind': 'reminder', 'reminder_id': reminder['id'], 'dog_user_id': dog['id'],
                              'day': day, 'timestamp': iso(now), 'next_attempt_at': iso(now), 'attempts': 0,
                              'status': 'queued' if now - scheduled <= timedelta(minutes=15) else 'missed', 'payload': payload}
                    await self.ctx.db.dispatches.insert_one(record, session=session)
                await self.ctx.database.transaction(create)

    async def run(self):
        while True:
            try:
                await self.schedule()
                for _ in range(20):
                    if not await self.robot.dispatch_one():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No URIs, payloads, or raw provider errors in logs.
                logger.warning('Background cycle failed (%s); retrying next cycle', type(exc).__name__)
            await asyncio.sleep(15)
