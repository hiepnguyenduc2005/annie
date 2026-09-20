import asyncio
from dataclasses import dataclass
from .schemas.common import iso, utcnow


@dataclass(eq=False)
class Subscriber:
    app_user_id: int
    dog_user_id: int
    queue: asyncio.Queue


class Events:
    def __init__(self, database):
        self.database = database
        self.subscribers = set()

    def subscribe(self, user):
        subscriber = Subscriber(user['id'], user['dog_user_id'], asyncio.Queue(maxsize=100))
        self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        self.subscribers.discard(subscriber)

    async def publish(self, kind, record, *, dog_user_id, app_user_id=None):
        if not self.subscribers:
            return
        event_id = await self.database.transaction(self.database.next_id)
        event = {'event_id': event_id, 'type': kind,
                 'timestamp': iso(utcnow()), 'data': record}
        for subscriber in list(self.subscribers):
            if subscriber.dog_user_id != dog_user_id or (app_user_id is not None and subscriber.app_user_id != app_user_id):
                continue
            if subscriber.queue.full():
                while not subscriber.queue.empty():
                    subscriber.queue.get_nowait()
                subscriber.queue.put_nowait({'type': 'resync_required'})
            else:
                subscriber.queue.put_nowait(event)
