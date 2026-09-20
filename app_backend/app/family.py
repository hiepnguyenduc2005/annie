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
                     'recalling', 'recalled', 'completed', 'failed', 'cancelled']

# completed/failed arrive as events; unreachable is dispatch failure; cancelled is a family pause.
RUN_TERMINAL = {'completed', 'failed', 'unreachable', 'cancelled'}
EVENT_TERMINAL = {'completed', 'failed', 'cancelled'}  # kinds that close a run when they arrive

# Inactivity bound for live runs: a run with no callback for this long is failed, never left
# "running" forever. Clamped so a typo cannot disable it or strand runs for hours.
DEFAULT_EVENT_DEADLINE_S = 120.0
EVENT_DEADLINE_MIN_S, EVENT_DEADLINE_MAX_S = 30.0, 900.0


def now_ms():
    return int(time.time() * 1000)


class MessageIn(StrictModel):
    author_id: Literal['jeanine', 'zach', 'ellis']
    text: Text
    reminder_id: int | None = None  # set by the app's "Remind" button: a delivered reminder updates the schedule


def classify_outcome(run, payload) -> dict:
    """The three app-facing outcomes of a family message (contract: family_messages.md):
    message response (delivered, with or without a reply), reminder update (a reminder acknowledged -> done),
    emergency (the person may need help)."""
    reply = str((payload or {}).get('reply') or 'none')
    detail = str((payload or {}).get('detail') or '')
    if run.get('status') == 'cancelled':
        return {'type': 'cancelled', 'delivered': False, 'reply': 'none',
                'detail': detail or 'Paused by family before this run finished.'}
    if run.get('status') == 'failed':
        return {'type': 'message_response', 'delivered': False, 'reply': 'none', 'detail': detail}
    if reply == 'concern':
        return {'type': 'emergency', 'delivered': True, 'reply': reply, 'detail': detail}
    if run.get('reminder_id') is not None and reply in ('okay', 'none'):
        return {'type': 'reminder_update', 'delivered': True, 'reply': reply, 'reminder_id': run['reminder_id'],
                'done': reply == 'okay', 'detail': detail}
    return {'type': 'message_response', 'delivered': True, 'reply': reply, 'detail': detail}


class InternalEventIn(StrictModel):
    run_id: UUID
    kind: EventKind
    payload: dict = Field(default_factory=dict)
    at: Timestamp


DEFAULT_RECALL = 'Your phone was on the living room couch, by the left cushion.'

# Who a given beat belongs to, for clients rendering the run as a conversation.
SPEAKER = {'speaking': 'annie', 'heard': 'resident'}


def summarize(kind, payload):
    """One display line per event.

    Clients render whatever this returns rather than reaching into `payload`,
    whose keys vary by kind and come from robot_backend, so an unexpected
    payload degrades to a readable line instead of breaking the UI.
    """
    payload = payload or {}

    def text(key, fallback):
        value = payload.get(key)
        return value if isinstance(value, str) and value.strip() else fallback

    if kind == 'speaking':
        return text('text', 'Annie said something.')
    if kind == 'heard':
        return text('transcript', 'Annie heard a reply.')
    if kind == 'recalled':
        return text('note', 'Annie recalled something.')
    if kind == 'navigating':
        return text('detail', 'On the way' + (f' to the {payload["waypoint"]}' if isinstance(payload.get('waypoint'), str) else '') + '.')
    if kind == 'arrived':
        return text('detail', 'Arrived.')
    if kind == 'listening':
        return 'Listening for a reply…'
    if kind == 'recalling':
        return 'Checking what Annie remembers…'
    if kind == 'completed':
        return text('detail', 'Finished.')
    if kind == 'failed':
        return text('error', 'Annie could not finish this one.')
    if kind == 'cancelled':
        return text('error', 'Annie was paused before finishing this one.')
    if kind == 'unreachable':
        return 'Could not reach Annie at home. Nothing was delivered.'
    return kind


def build_mock_sequence(text, author_name, recall=DEFAULT_RECALL):
    # Stand-in cadence for demoing without the GX10; real hardware timing is
    # the 60-90s sequence described in contract/family_messages.md, not this.
    # The relayed line quotes the sender verbatim rather than paraphrasing:
    # paraphrasing is the planner's job on the robot, and inventing one here
    # would misrepresent what the mock actually does.
    return [
        (2.0, 'navigating', {'waypoint': 'living-room', 'detail': 'Looking for Jeanine.'}),
        (3.0, 'arrived', {'waypoint': 'living-room', 'detail': 'Found Jeanine in the living room.'}),
        (3.0, 'speaking', {'text': f'Jeanine, it\'s Annie. {author_name} asked me to pass this along: '
                                   f'"{text}"'}),
        (3.0, 'listening', {}),
        (2.5, 'heard', {'transcript': 'Oh dear, I forgot where I put it.'}),
        (2.0, 'recalling', {'query': 'where was the phone last seen'}),
        (2.0, 'recalled', {'note': recall, 'source': 'observation memory'}),
        (3.0, 'speaking', {'text': 'You left it on the living room couch yesterday afternoon, '
                                   'by the left cushion. It may have slipped into the crack.'}),
        (1.0, 'completed', {'detail': 'Jeanine is going to check the couch.'}),
    ]


async def default_dispatch(url, payload, timeout, headers=None):
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=payload, headers=headers or {})


class FamilyService:
    def __init__(self, clock=now_ms, robot_backend_url='', dispatch_timeout=3.0,
                 mock=False, mock_speed=1.0, dispatch_fn=None, recall_provider=None, internal_secret='', on_outcome=None,
                 event_deadline_s=DEFAULT_EVENT_DEADLINE_S):
        self.clock = clock
        self.on_outcome = on_outcome  # callback(run) after a terminal event: reminder update / emergency outcomes
        self.robot_backend_url = robot_backend_url.rstrip('/')
        self.dispatch_timeout = dispatch_timeout
        self.mock = mock
        self.mock_speed = mock_speed
        self.event_deadline_s = max(EVENT_DEADLINE_MIN_S, min(EVENT_DEADLINE_MAX_S, float(event_deadline_s)))
        self.paused = False  # set by pause(); a new explicit family message clears it (resume is deliberate)
        self.pausing = 0  # concurrent pause requests still awaiting the robot stop
        self.internal_secret = internal_secret or ''
        if dispatch_fn is None and self.internal_secret:
            # robot_backend authenticates /dispatch with the same shared secret it uses to post events back
            secret = self.internal_secret
            dispatch_fn = lambda url, payload, timeout: default_dispatch(url, payload, timeout,  # noqa: E731
                                                                         {'X-Internal-Secret': secret})
        self.dispatch_fn = dispatch_fn or default_dispatch
        # Lets the mock cite the same observation the resident view shows,
        # instead of a second hardcoded copy that could drift out of sync.
        self.recall_provider = recall_provider
        self.thread = []  # message dicts, oldest first
        self.runs = {}  # run_id -> run dict
        self.run_tasks = {}  # run_id -> asyncio.Task driving its dispatch/mock sequence
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


    def post_message(self, author_id, text, reminder_id=None):
        # Exact-duplicate guard: a retrying/tapping client must not pile identical errands onto the
        # dog's FIFO queue. The still-active run with the same author, text and reminder is the answer.
        for existing in self.runs.values():
            if (existing['status'] not in RUN_TERMINAL and existing['author_id'] == author_id
                    and existing['text'] == text and existing.get('reminder_id') == reminder_id):
                message = next((m for m in self.thread if m['run_id'] == existing['run_id']), None)
                if message is not None:
                    return message, existing, False
        self.paused = False  # an explicit new message is the only resume after a pause
        run_id = str(uuid4())
        at = self.clock()
        message = {'message_id': str(uuid4()), 'author_id': author_id, 'text': text, 'at': at, 'run_id': run_id}
        run = {'run_id': run_id, 'author_id': author_id, 'text': text, 'status': 'dispatched',
               'created_at': at, 'updated_at': at, 'events': [], 'reminder_id': reminder_id, 'outcome': None}
        self.thread.append(message)
        self.runs[run_id] = run
        self.emit('message', message)
        self.emit('run_status', {'run_id': run_id, 'status': run['status']})
        task = self._spawn(self._dispatch(run_id, author_id, text))
        self.run_tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self.run_tasks.pop(rid, None))
        return message, run, True

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def dispatch_schema_message(self, message):
        """Hand a stored message's text to robot_backend. Same rules as a run
        dispatch: never block the request, and a failure is recorded rather
        than raised, because the message is already saved."""
        self._spawn(self._post_to_robot('/messages', {
            'message_id': message['id'],
            'dog_user_id': message['dog_user_id'],
            'app_user_id': message['app_user_id'],
            'text': message['texts'][0]['text'],
        }))

    def dispatch_reminder(self, reminder):
        """Send a reminder's description to robot_backend. The robot reports
        what actually happened to /internal/reminder-history."""
        self._spawn(self._post_to_robot('/reminders', {
            'reminder_id': reminder['id'],
            'dog_user_id': reminder['dog_user_id'],
            'hour': reminder['hour'],
            'item': reminder['item'],
        }))

    async def _deliver(self, path, payload):
        """One attempt plus one retry against robot_backend.

        Returns (delivered, last_error, robot_state). robot_state is the "state"
        field of a 2xx JSON body when present (/dispatch answers "queued" when
        the errand FIFO is busy). The retry policy lives here alone so it
        cannot drift between the run dispatch and the record dispatches.
        """
        url = f'{self.robot_backend_url}{path}'
        last_error = 'unknown dispatch error'
        for attempt in range(2):
            try:
                response = await self.dispatch_fn(url, payload, self.dispatch_timeout)
                if 200 <= response.status_code < 300:
                    return True, None, self._response_state(response)
                last_error = f'robot_backend returned {response.status_code}'
            except httpx.HTTPError as exc:
                last_error = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
            if attempt == 0:
                await asyncio.sleep(0.5)
        return False, last_error, None

    @staticmethod
    def _response_state(response):
        try:
            body = response.json()
        except Exception:
            return None  # fakes and legacy stand-ins have no JSON body
        state = body.get('state') if isinstance(body, dict) else None
        return state if isinstance(state, str) else None

    async def _post_to_robot(self, path, payload):
        """Send a stored record onward. A failure is announced but not raised:
        the record is already saved, and the family app must not fail because
        the dog is unreachable."""
        if self.mock or not self.robot_backend_url:
            return False
        delivered, _, _state = await self._deliver(path, payload)
        if not delivered:
            self.emit('robot_unreachable', {
                'path': path,
                'payload_id': payload.get('message_id') or payload.get('reminder_id')})
        return delivered

    async def _dispatch(self, run_id, author_id, text):
        """Send a family message's errand to the robot and record the outcome
        on its run."""
        if self.mock:
            await self._run_mock_sequence(run_id, text, HOUSEHOLD[author_id]['name'])
            return
        if not self.robot_backend_url:
            self._mark_unreachable(run_id, 'ROBOT_BACKEND_URL is not configured')
            return
        delivered, error, robot_state = await self._deliver('/dispatch', {
            'run_id': run_id, 'author_id': author_id,
            'author_name': HOUSEHOLD[author_id]['name'],
            'text': text, 'dispatched_at': self.clock()})
        if not delivered:
            self._mark_unreachable(run_id, error)
        elif robot_state == 'queued':
            # The errand FIFO is busy: the run is accepted but not started. Only a real
            # callback moves a run to 'running'; an HTTP 202 alone never does. A callback
            # that raced the 202 must not be downgraded back to queued.
            run = self.runs.get(run_id)
            if run is not None and run['status'] == 'dispatched':
                self._update_status(run_id, 'queued')

    def _mark_unreachable(self, run_id, error):
        run = self.runs.get(run_id)
        if run is None or run['status'] in RUN_TERMINAL:
            return
        run['status'] = 'unreachable'
        run['updated_at'] = self.clock()
        payload = {'error': error}
        event = {'event_id': str(uuid4()), 'run_id': run_id, 'kind': 'unreachable', 'payload': payload,
                 'at': run['updated_at'], 'summary': summarize('unreachable', payload), 'speaker': 'system'}
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
        event = {'event_id': str(uuid4()), 'run_id': run_id, 'kind': kind, 'payload': payload, 'at': at,
                 'summary': summarize(kind, payload), 'speaker': SPEAKER.get(kind, 'system')}
        run['events'].append(event)
        run['updated_at'] = self.clock()
        run['status'] = kind if kind in EVENT_TERMINAL else 'running'
        if kind in EVENT_TERMINAL:
            run['outcome'] = classify_outcome(run, payload)
            if self.on_outcome is not None:
                try:
                    self.on_outcome(run)
                except Exception:
                    pass
        self.emit('run_event', event)
        self.emit('run_status', {'run_id': run_id, 'status': run['status']})
        return event

    def tick(self):
        """Inactivity deadline, driven by the app's ticker. A live run (dispatched, queued or
        running) that has heard nothing for event_deadline_s is failed with a synthetic event:
        "queued forever" and stranded callbacks must surface, never hang the phone's view."""
        now = self.clock()
        deadline_ms = self.event_deadline_s * 1000
        for run in list(self.runs.values()):
            if run['status'] in ('dispatched', 'queued', 'running') and now - run['updated_at'] > deadline_ms:
                seconds = int(self.event_deadline_s)
                payload = {'error': f'No word from Annie for {seconds} s; treating this run as failed. '
                                    f'The message may not have been delivered.'}
                run['status'] = 'failed'
                run['updated_at'] = now
                run['outcome'] = classify_outcome(run, payload)
                event = {'event_id': str(uuid4()), 'run_id': run['run_id'], 'kind': 'failed', 'payload': payload,
                         'at': now, 'summary': summarize('failed', payload), 'speaker': 'system'}
                run['events'].append(event)
                self.emit('run_event', event)
                self.emit('run_status', {'run_id': run['run_id'], 'status': 'failed'})

    async def pause(self):
        self.pausing += 1
        try:
            return await self._pause()
        finally:
            self.pausing -= 1

    async def _pause(self):
        """Family Pause: cancel every live run and ask the errand service to stop the dog.

        Cancelling is local and total (dispatched/queued/running -> cancelled, terminal); the
        errand's /pause cancels its active errand, clears its FIFO and issues the software body
        stop. stop_confirmed is false whenever that stop was not acknowledged, so clients treat
        motion state as unknown. Resume is a new explicit message, never an automatic replay.
        """
        self.paused = True
        cancelled = []
        for run in list(self.runs.values()):
            if run['status'] in RUN_TERMINAL:
                continue
            run['status'] = 'cancelled'
            run['updated_at'] = self.clock()
            run['outcome'] = {'type': 'cancelled', 'delivered': False, 'reply': 'none',
                              'detail': 'Paused by family before this run finished.'}
            cancelled.append(run['run_id'])
            self.emit('run_status', {'run_id': run['run_id'], 'status': 'cancelled'})
        for _run_id, task in list(self.run_tasks.items()):
            task.cancel()
        self.run_tasks.clear()
        stop_confirmed = await self._request_robot_stop()
        return {'paused': True, 'cancelled_runs': cancelled, 'stop_confirmed': stop_confirmed}

    async def _request_robot_stop(self):
        if self.mock:
            return True  # nothing physical was dispatched; in-process tasks are already cancelled
        if not self.robot_backend_url:
            return False  # no errand service to confirm against; runs were cancelled locally
        try:
            # The errand's stop budget is the body's 2.5 s acknowledgement plus queue
            # draining; give the pause receipt at least 4 s even on a tighter dispatch timeout.
            response = await self.dispatch_fn(f'{self.robot_backend_url}/pause', {},
                                              max(self.dispatch_timeout, 4.0))
        except Exception:
            return False
        if not 200 <= response.status_code < 300:
            return False
        try:
            body = response.json()
        except Exception:
            return False  # an unparseable receipt is not a confirmed stop
        # Strict: only an explicit JSON true confirms the software stop. A missing key,
        # a non-dict body, or a truthy string like "false" all mean unconfirmed.
        return isinstance(body, dict) and body.get('stop_confirmed') is True

    async def _run_mock_sequence(self, run_id, text, author_name):
        self._update_status(run_id, 'running')
        recall = (self.recall_provider and self.recall_provider()) or DEFAULT_RECALL
        for delay, kind, payload in build_mock_sequence(text, author_name, recall):
            await asyncio.sleep(delay * self.mock_speed)
            try:
                self.add_event(run_id, kind, payload, self.clock())
            except (KeyError, ValueError):
                return
