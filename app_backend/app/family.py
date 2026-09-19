"""In-memory family messaging: thread, async robot-dispatch runs, live fan-out.

Deliberately separate from Service's SQLite state: this feature is explicitly
scoped in-memory-only (no persistence across restarts), per product decision.
See contract/family_messages.md for the app_backend<->robot_backend interface
this drives.
"""
import asyncio
import time
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from .models import StrictModel, Text, Timestamp

HOUSEHOLD = {
    'jeanine': {'name': 'Jeanine', 'role': 'resident'},
    'zach': {'name': 'Zach', 'role': 'family'},
    'ellis': {'name': 'Ellis', 'role': 'family'},
}

EventKind = Literal['navigating', 'arrived', 'speaking', 'listening', 'heard',
                     'recalling', 'recalled', 'completed', 'failed']

RUN_TERMINAL = {'completed', 'failed', 'unreachable'}


def now_ms():
    return int(time.time() * 1000)


class MessageIn(StrictModel):
    author_id: Literal['jeanine', 'zach', 'ellis']
    text: Text


class InternalEventIn(StrictModel):
    run_id: UUID
    kind: EventKind
    payload: dict = Field(default_factory=dict)
    at: Timestamp


def build_mock_sequence(text, author_name):
    # Stand-in cadence for demoing without the GX10; real hardware timing is
    # the 60-90s sequence described in contract/family_messages.md, not this.
    return [
        (1.5, 'navigating', {'waypoint': 'jeanine'}),
        (2.5, 'arrived', {'waypoint': 'jeanine'}),
        (2.0, 'speaking', {'text': f'{author_name} says: {text}'}),
        (3.0, 'listening', {}),
        (2.0, 'heard', {'transcript': "Oh how lovely, tell them thank you, I'm doing just fine today."}),
        (1.5, 'recalling', {'query': 'recent notes about Jeanine'}),
        (1.5, 'recalled', {'note': 'Jeanine watered the tomatoes yesterday afternoon.'}),
        (2.0, 'speaking', {'text': f'{author_name}, she says thank you, and that she watered the tomatoes yesterday.'}),
        (0.5, 'completed', {}),
    ]


async def default_dispatch(url, payload, timeout):
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=payload)


class FamilyService:
    def __init__(self, clock=now_ms, robot_backend_url='', dispatch_timeout=3.0,
                 mock=False, mock_speed=1.0, dispatch_fn=None):
        self.clock = clock
        self.robot_backend_url = robot_backend_url.rstrip('/')
        self.dispatch_timeout = dispatch_timeout
        self.mock = mock
        self.mock_speed = mock_speed
        self.dispatch_fn = dispatch_fn or default_dispatch
        self.thread = []  # message dicts, oldest first
        self.runs = {}  # run_id -> run dict
        self.subscribers = set()
        self.background_tasks = set()

    def emit(self, type_, data):
        for queue in tuple(self.subscribers):
            if queue.full():
                # Never leave a connected client with a stale run/thread view;
                # REST (/api/thread, /api/runs/{id}) is the source of truth.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({'type': 'resync', 'data': {'reason': 'subscriber_overflow'}})
                continue
            queue.put_nowait({'type': type_, 'data': data})

    def recent_history(self, limit=50):
        return {'thread': self.thread[-limit:], 'runs': list(self.runs.values())[-limit:]}

    def post_message(self, author_id, text):
        run_id = str(uuid4())
        at = self.clock()
        message = {'message_id': str(uuid4()), 'author_id': author_id, 'text': text, 'at': at, 'run_id': run_id}
        run = {'run_id': run_id, 'author_id': author_id, 'text': text, 'status': 'dispatched',
               'created_at': at, 'updated_at': at, 'events': []}
        self.thread.append(message)
        self.runs[run_id] = run
        self.emit('message', message)
        self.emit('run_status', {'run_id': run_id, 'status': run['status']})
        task = asyncio.create_task(self._dispatch(run_id, author_id, text))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return message, run

    async def _dispatch(self, run_id, author_id, text):
        if self.mock:
            await self._run_mock_sequence(run_id, text, HOUSEHOLD[author_id]['name'])
            return
        if not self.robot_backend_url:
            self._mark_unreachable(run_id, 'ROBOT_BACKEND_URL is not configured')
            return
        payload = {'run_id': run_id, 'author_id': author_id, 'author_name': HOUSEHOLD[author_id]['name'],
                   'text': text, 'dispatched_at': self.clock()}
        url = f'{self.robot_backend_url}/dispatch'
        last_error = 'unknown dispatch error'
        for attempt in range(2):  # one attempt plus one retry
            try:
                response = await self.dispatch_fn(url, payload, self.dispatch_timeout)
                if 200 <= response.status_code < 300:
                    self._update_status(run_id, 'running')
                    return
                last_error = f'robot_backend returned {response.status_code}'
            except httpx.HTTPError as exc:
                last_error = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
            if attempt == 0:
                await asyncio.sleep(0.5)
        self._mark_unreachable(run_id, last_error)

    def _mark_unreachable(self, run_id, error):
        run = self.runs.get(run_id)
        if run is None or run['status'] in RUN_TERMINAL:
            return
        run['status'] = 'unreachable'
        run['updated_at'] = self.clock()
        event = {'event_id': str(uuid4()), 'run_id': run_id, 'kind': 'unreachable', 'payload': {'error': error}, 'at': run['updated_at']}
        run['events'].append(event)
        self.emit('run_event', event)
        self.emit('run_status', {'run_id': run_id, 'status': run['status']})

    def _update_status(self, run_id, status):
        run = self.runs.get(run_id)
        if run is None or run['status'] in RUN_TERMINAL:
            return
        run['status'] = status
        run['updated_at'] = self.clock()
        self.emit('run_status', {'run_id': run_id, 'status': status})

    def add_event(self, run_id, kind, payload, at):
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run['status'] in RUN_TERMINAL:
            raise ValueError(f'run {run_id} is already {run["status"]}')
        event = {'event_id': str(uuid4()), 'run_id': run_id, 'kind': kind, 'payload': payload, 'at': at}
        run['events'].append(event)
        run['updated_at'] = self.clock()
        run['status'] = kind if kind in ('completed', 'failed') else 'running'
        self.emit('run_event', event)
        self.emit('run_status', {'run_id': run_id, 'status': run['status']})
        return event

    async def _run_mock_sequence(self, run_id, text, author_name):
        self._update_status(run_id, 'running')
        for delay, kind, payload in build_mock_sequence(text, author_name):
            await asyncio.sleep(delay * self.mock_speed)
            try:
                self.add_event(run_id, kind, payload, self.clock())
            except (KeyError, ValueError):
                return
