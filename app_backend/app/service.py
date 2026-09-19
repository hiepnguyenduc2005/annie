"""Single-process local demo state, bounded memory and deterministic safety rules."""
import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from uuid import uuid4
from .models import CHANNEL_MODELS, Event, Perception, VoiceHeard


def now_ms():
    return int(time.time() * 1000)


class Service:
    def __init__(self, db_path, mode='demo', clock=now_ms):
        self.clock, self.mode = clock, mode
        if db_path != ':memory:':
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
        self.db.commit()
        self.dog = self.perception = self.map = self.pending = None
        self.last_ts = self.db.execute('SELECT COALESCE(MAX(ts), -1) FROM memory').fetchone()[0]
        self.candidate = None
        self.candidate_map = None
        self.episode = False
        self.commands = []
        self.subscribers = set()
        self.crops = {}  # Crop bytes only, populated by a future trusted crop adapter.

    def close(self):
        self.db.close()

    def emit(self, channel, data):
        for queue in tuple(self.subscribers):
            if queue.full():
                # Never silently leave a connected consumer with stale incident state.
                # Replace the backlog with one bounded notice; REST is the source of truth.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({'type': 'resync', 'data': {'reason': 'subscriber_overflow'}})
                continue
            queue.put_nowait({'type': channel, 'data': data})

    def status(self):
        return {'dog': self.dog, 'perception': self.perception, 'mode': self.mode,
                'pending_checkin': self.pending,
                'integrations': {'robot': ('simulation_connected' if self.clock() - self.dog['ts'] < 3000 else 'simulation_stale')
                                 if self.dog and self.dog['pose']['map_id'].startswith('sim-') else 'not_connected', 'redis': 'not_implemented',
                                 'memory': 'local_sqlite', 'voice': 'queued_only', 'vision': 'ingest_only'}}

    def events(self, since=0):
        return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM events WHERE ts > ? ORDER BY ts, id', (since,))]

    def event(self, kind, severity, evidence, ts=None):
        event = Event(ts=self.clock() if ts is None else ts, event_id=uuid4(), kind=kind, severity=severity, evidence=evidence).model_dump(mode='json')
        self.db.execute('INSERT INTO events VALUES (?, ?, ?)', (event['event_id'], event['ts'], json.dumps(event)))
        self.db.commit()
        self.emit('event', event)
        return event

    def ack(self, event_id, by):
        row = self.db.execute('SELECT payload FROM events WHERE id=?', (event_id,)).fetchone()
        if not row:
            raise KeyError(event_id)
        event = json.loads(row[0])
        if not event['acknowledged']:
            event.update(acknowledged=True, acknowledged_by=by, acknowledged_at=self.clock())
            self.db.execute('UPDATE events SET payload=? WHERE id=?', (json.dumps(event), event_id))
            self.db.commit()
            self.emit('event', event)
        return event

    def queue_command(self, data):
        item = {'command_id': str(uuid4()), 'ts': self.clock(), 'status': 'queued', **data}
        self.commands.append(item)
        self.commands = self.commands[-100:]
        self.emit('command', item)
        return item

    def command_receipt(self, command_id, status, source, detail=None):
        # Simulator bridge only reports what actually ran; the queue records it.
        item = next((entry for entry in self.commands if entry['command_id'] == command_id), None)
        if item is None:
            raise KeyError(command_id)
        allowed = {'queued': {'accepted', 'executing', 'failed'},
                   'accepted': {'executing', 'completed', 'failed'},
                   'executing': {'completed', 'failed'},
                   'completed': {'completed'}, 'failed': {'failed'}}[item['status']]
        if status not in allowed:
            raise ValueError(f'command status {item["status"]} cannot transition to {status}')
        if item['status'] != status:
            item.update(status=status, source=source, detail=detail, updated_at=self.clock())
            self.emit('command', item)
        return item

    def ingest(self, channel, data):
        model = CHANNEL_MODELS[channel].model_validate(data)
        payload = model.model_dump(mode='json')
        if channel == 'brain.perception':
            return self.observe(model)
        if channel == 'voice.heard':
            self.reply(model)
        elif channel == 'dog.status':
            self.dog = payload
        elif channel == 'dog.map':
            if self.candidate_map != payload['map_id']:
                self.candidate = self.candidate_map = None
            self.map = payload
        self.emit(channel, payload)
        return True

    def observe(self, frame: Perception):
        now = self.clock()
        # Out-of-order, stale, future, and replayed frames never update rules or memory.
        # Millisecond precision can give distinct captures equal timestamps.
        # Arrival order resolves ties; the persisted UUID rejects replayed frames.
        if frame.ts < self.last_ts or not 0 <= now - frame.ts <= 5000:
            return False
        if self.map and frame.pose.map_id != self.map['map_id']:
            return False
        payload = frame.model_dump(mode='json')
        cursor = self.db.execute('INSERT OR IGNORE INTO memory VALUES (?, ?, ?)', (str(frame.frame_id), frame.ts, json.dumps(payload)))
        if not cursor.rowcount:
            return False
        self.db.execute('DELETE FROM memory WHERE id NOT IN (SELECT id FROM memory ORDER BY ts DESC, rowid DESC LIMIT 1000)')
        self.db.commit()
        self.last_ts, self.perception = frame.ts, payload
        self.emit('brain.perception', payload)
        risky = frame.person and frame.posture == 'lying' and frame.location in ('floor', 'chair') and frame.confidence >= .8
        safe = frame.confidence >= .8 and (not frame.person or (frame.posture != 'unknown' and frame.location != 'unknown' and not risky))
        if not risky:
            self.candidate = self.candidate_map = None
            if safe and self.pending is None:
                self.episode = False
            return True
        if self.episode:
            return True
        previous = self.candidate if self.candidate_map == frame.pose.map_id else None
        self.candidate, self.candidate_map = frame.ts, frame.pose.map_id
        if previous is None or frame.ts - previous > 5000:
            return True
        self.episode = True
        evidence = {'frame_id': str(frame.frame_id), 'pose': payload['pose'], 'crop_url': None}
        event = self.event('fall_suspected', 'warn', evidence)
        self.pending = {'event_id': event['event_id'], 'started_at': now, 'deadline_at': now + 8000}
        self.pending_evidence = evidence
        self.queue_command({'text': 'Are you okay? Please say okay or help.', 'cmd': 'say'})
        self.emit('checkin', self.pending)
        return True

    def reply(self, voice: VoiceHeard):
        pending = self.pending
        if not pending or voice.confidence < .8 or str(voice.event_id) != pending['event_id']:
            return
        now = self.clock()
        if not pending['started_at'] <= voice.ts <= min(now, pending['deadline_at']) or now >= pending['deadline_at']:
            self.tick(now)
            return
        text = re.sub(r'[^a-z ]', '', voice.text.lower()).strip()
        if text in ('help', 'help me', 'i need help'):
            self.escalate(now)
        elif text in ('okay', 'ok', 'im okay', 'i am okay', 'im ok', 'i am ok'):
            self.event('checkin_ok', 'info', self.pending_evidence)
            self.pending = None
            self.emit('checkin', None)

    def escalate(self, now):
        if self.pending:
            self.event('fall_confirmed', 'critical', self.pending_evidence)
            self.pending = None
            self.emit('checkin', None)

    def tick(self, now=None):
        now = self.clock() if now is None else now
        if self.pending and now >= self.pending['deadline_at']:
            # `now` can be an explicit demo deadline; event timestamps remain wall time.
            self.event('checkin_no_reply', 'warn', self.pending_evidence)
            self.escalate(now)

    def query(self, text):
        stop = {'where', 'when', 'was', 'the', 'a', 'an', 'is', 'did', 'i', 'my', 'you', 'see', 'last', 'what', 'me', 'for', 'of', 'in', 'at', 'to'}
        words = set(re.findall(r'\w+', text.lower())) - stop
        matches = []
        for row in self.db.execute('SELECT payload FROM memory ORDER BY ts DESC, rowid DESC LIMIT 1000'):
            item = json.loads(row[0])
            score = len(words & set(re.findall(r'\w+', item['caption'].lower())))
            if score:
                matches.append((score, item))
        matches.sort(key=lambda pair: (pair[0], pair[1]['ts']), reverse=True)
        found = [item for _, item in matches[:3]]
        return {'answer': ' '.join(item['caption'] for item in found) if found else 'I wasn’t there for that',
                'answerable': bool(found), 'citations': [{'frame_id': item['frame_id'], 'ts': item['ts'], 'pose': item['pose'], 'crop_url': None} for item in found]}

    def seed(self):
        now = self.clock()
        self.ingest('dog.map', {'ts': now, 'map_id': 'demo-home', 'origin': {'x': 0, 'y': 0}, 'resolution_m': .05,
            'waypoints': [{'id': 'living-room', 'x': 2, 'y': 2}, {'id': 'bedroom', 'x': 6, 'y': 2}, {'id': 'hallway', 'x': 4, 'y': 1}],
            'rooms': [{'id': 'living-room', 'label': 'Living room', 'x': 0, 'y': 0, 'width': 4, 'height': 4}, {'id': 'bedroom', 'label': 'Bedroom', 'x': 4, 'y': 0, 'width': 4, 'height': 4}]})
        self.ingest('dog.status', {'ts': now, 'state': 'idle', 'battery_pct': 84, 'pose': {'x': 2, 'y': 2}})
        self.scenario('safe_bed')

    def scenario(self, name):
        now = self.clock()
        if name in ('help', 'okay'):
            if self.pending:
                self.ingest('voice.heard', {'ts': now, 'text': name, 'confidence': .99, 'event_id': self.pending['event_id']})
        elif name == 'timeout':
            if self.pending:
                # Explicit simulated deadline, never wait eight seconds in the demo control.
                self.tick(self.pending['deadline_at'])
        else:
            for ts in ([now, now] if name == 'fall' else [now]):
                self.ingest('brain.perception', {'ts': ts, 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying',
                    'location': 'floor' if name == 'fall' else 'bed', 'confidence': .95,
                    'caption': 'Demo: resident lying on the floor beside the blue mug.' if name == 'fall' else 'Demo: resident resting on the bed; glasses on the bedside table.',
                    'pose': {'x': 6, 'y': 2}})
