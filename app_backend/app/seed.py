from datetime import timedelta
from zoneinfo import ZoneInfo
from .schemas.common import iso, utcnow

REMINDERS = [
    (4, '08:00', 'Take morning medication'),
    (5, '09:30', 'Morning walk with Annie'),
    (6, '12:30', 'Take midday medication'),
    (7, '15:00', 'Charge your phone'),
    (8, '18:00', 'Take evening medication'),
]


async def seed(database, clock=utcnow):
    """All seed checks/writes commit together, so interrupted startup is safe."""
    async def initialize(session):
        db = database.db
        # Existing initialization is never re-run after users delete demo records.
        if await db.metadata.find_one({'_id': 'seed_v2'}, session=session):
            return
        now = clock()
        stamp = iso(now)
        day = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
        tomorrow = (now.astimezone(ZoneInfo('America/New_York')).date() + timedelta(days=1)).isoformat()
        if not await db.dog_users.find_one({}, session=session):
            await db.dog_users.insert_one({'id': 1, 'name': 'Jeanine', 'timezone': 'America/New_York', 'timestamp': stamp}, session=session)
        resident = await db.dog_users.find_one({'id': 1, 'name': 'Jeanine'}, session=session)
        if resident and not await db.app_users.find_one({}, session=session):
            await db.app_users.insert_many([{'id': 2, 'name': 'Zach', 'dog_user_id': 1, 'timestamp': stamp},
                                           {'id': 3, 'name': 'Ellis', 'dog_user_id': 1, 'timestamp': stamp}], session=session)
        seeded_reminders = False
        if resident and not await db.reminders.find_one({}, session=session):
            await db.reminders.insert_many([{'id': id, 'dog_user_id': 1, 'daily_time': time, 'description': text,
                                            'timestamp': stamp, 'enabled': True, '_schedule_from': tomorrow}
                                           for id, time, text in REMINDERS], session=session)
            seeded_reminders = True
        if seeded_reminders and not await db.notes.find_one({}, session=session):
            for note_id, request_id, reminder_id in [(9, 11, 4), (10, 12, 5)]:
                await db.dispatches.insert_one({'id': request_id, 'kind': 'reminder', 'status': 'seed',
                                                'reminder_id': reminder_id, 'dog_user_id': 1, 'day': day,
                                                'timestamp': stamp}, session=session)
                await db.notes.insert_one({'id': note_id, 'request_id': request_id, 'occurrence_id': request_id,
                                          'reminder_id': reminder_id, 'dog_user_id': 1, 'day': day, 'timestamp': stamp,
                                          'description': 'Synthetic seed example: completed.',
                                          'outcome': 'completed', 'source': 'seed'}, session=session)
        await db.metadata.insert_one({'_id': 'seed_v2', 'timestamp': stamp}, session=session)
    await database.transaction(initialize)
