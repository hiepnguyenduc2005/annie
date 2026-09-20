"""Single-process local demo state, bounded memory and deterministic safety rules."""
import copy
import asyncio
import json
import os
import re
import sqlite3
import time
import functools
from pathlib import Path
from uuid import uuid4
from .models import CHANNEL_MODELS, Event, Perception, VoiceHeard
from .semantic_memory import SemanticMemory, disabled_embedder, ollama_embedder
from . import audio_reply


def now_ms():
    return int(time.time() * 1000)


def embedder_from_env():
    # Unset or "off" keeps recall on the offline lexical fallback; captions are
    # sent to an embedding provider only when one is explicitly configured.
    model = os.getenv('ANNIE_EMBED_MODEL', '').strip()
    if not model or model.lower() == 'off':
        return disabled_embedder
    return ollama_embedder(os.getenv('ANNIE_EMBED_URL', '').strip() or 'http://127.0.0.1:11434', model)


AUDIO_WATCHDOG_MS = 8000  # Uncertain playback failure deadline (acceptance v1).
REPLY_WINDOW_MS = 8000
MAX_COMMANDS = 100


def atomic(name):
    """Reentrant transaction boundary for public mutation entry points.

    The outermost call opens one SQLite transaction, buffers subscriber
    emissions, and on success commits before any observer can see the
    updates. Helpers therefore commit only when no transaction is open.
    On exception the transaction rolls back and the snapshotted in-memory
    transactional state is restored, leaving persisted and in-memory views
    consistent.
    """
    def decorate(operation):
        @functools.wraps(operation)
        def wrapper(self, *args, **kwargs):
            self._atomic_depth += 1
            if self._atomic_depth > 1:
                try:
                    return operation(self, *args, **kwargs)
                finally:
                    self._atomic_depth -= 1
            snapshot = {field: copy.deepcopy(getattr(self, field)) for field in self._TRANSACTIONAL}
            self._emitting_suspended = True
            self._pending_emits = []
            try:
                result = operation(self, *args, **kwargs)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                for field, value in snapshot.items():
                    setattr(self, field, value)
                raise
            finally:
                self._atomic_depth -= 1
                self._emitting_suspended = False
            queued, self._pending_emits = self._pending_emits, []
            for channel, data in queued:
                self.emit(channel, data)
            return result
        return wrapper
    return decorate


class Service:
    def __init__(self, db_path, mode='demo', clock=now_ms, require_audio_receipt=False, embedder=None):
        self.clock, self.mode = clock, mode
        self.require_audio_receipt = require_audio_receipt
        if db_path != ':memory:':
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, ts INTEGER, payload TEXT)')
        # Small durable state: one row per logical topic (episode, pending check-in).
        self.db.execute('CREATE TABLE IF NOT EXISTS state (topic TEXT PRIMARY KEY, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY, seq INTEGER, payload TEXT)')
        self.db.commit()
        # Goal-conditioned recall over the memory table; an injected embedder
        # keeps tests offline, otherwise the provider comes from the environment.
        self.semantic = SemanticMemory(self.db, embedder=embedder or embedder_from_env())
        self.dog = self.perception = self.map = self.pending = None
        self.last_ts = self.db.execute('SELECT COALESCE(MAX(ts), -1) FROM memory').fetchone()[0]
        self.candidate = self.candidate_map = None
        self.rearm = []  # Fresh affirmative non-risky captures for episode re-arming.
        self.episode = False
        self._load_state()
        self.commands = []
        self._load_commands()
        self.subscribers = set()
        self.crops = {}  # Crop bytes only, populated by a future trusted crop adapter.
        # Transaction bookkeeping must exist before any atomic entry point runs.
        self._atomic_depth = 0
        self._emitting_suspended = False
        self._pending_emits = []
        # State restored atomically with the database on rollback.
        self._TRANSACTIONAL = ('pending', 'pending_evidence', 'episode', 'rearm', 'commands',
                               'candidate', 'candidate_map', 'last_ts', 'perception', 'dog', 'map')

    # ------------------------------------------------------------------
    # Durable state helpers (explicit saves at mutation boundaries only).
    # ------------------------------------------------------------------
    def _save_state(self, topic, value):
        if value is None:
            self.db.execute('DELETE FROM state WHERE topic=?', (topic,))
        else:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (topic, json.dumps(value)))
        # Inside an atomic operation the outermost wrapper owns the commit;
        # committing here would partially persist a half-done transaction.
        if not self._atomic_depth:
            self.db.commit()

    def _load_state(self):
        self.episode = False
        self.pending_evidence = None
        self.pending = None
        rows = dict(self.db.execute('SELECT topic, payload FROM state').fetchall())
        if 'episode' in rows:
            self.episode = json.loads(rows['episode'])
        if 'pending' in rows:
            stored = json.loads(rows['pending'])
            self.pending_evidence = stored.get('evidence')
            self.pending = stored.get('pending')

    def _save_pending(self):
        self._save_state('pending', None if self.pending is None else
                         {'pending': self.pending, 'evidence': self.pending_evidence})

    def _save_episode(self):
        self._save_state('episode', self.episode)

    @atomic('reset_demo_episode')
    def reset_demo_episode(self):
        """Explicitly re-arm a resolved demo without inventing recovery evidence."""
        if self.mode != 'demo':
            raise PermissionError('Demo controls are unavailable in live mode')
        if self.pending is not None:
            raise ValueError('Resolve the active check-in before resetting the demo episode')
        self.episode = False
        self.candidate = self.candidate_map = None
        self.rearm = []
        self._save_episode()
        return {'reset': True, 'scope': 'resolved_demo_episode'}

    def _load_commands(self):
        self.commands = [json.loads(row[0]) for row in
                         self.db.execute('SELECT payload FROM commands ORDER BY seq, rowid')]

    def _save_command(self, item):
        if item is None:
            return
        # Receipts UPDATE in place so rowid keeps stable queue insertion order.
        cursor = self.db.execute('UPDATE commands SET payload=? WHERE command_id=?',
                                 (json.dumps(item), item['command_id']))
        if cursor.rowcount:
            return
        self.db.execute('INSERT INTO commands VALUES (?, ?, ?)',
                        (item['command_id'], item.get('seq', self.clock()), json.dumps(item)))
        self._prune_commands()

    def _prune_commands(self):
        # The durable queue mirrors the bounded in-RAM list (100 entries) but
        # never evicts the pending check-in question needed for recovery.
        say_id = (self.pending or {}).get('say_command_id') if self.pending else None
        rows = list(self.db.execute('SELECT rowid, command_id FROM commands ORDER BY seq DESC, rowid DESC'))
        keep_ids = {command_id for _, command_id in rows[:MAX_COMMANDS]}
        if say_id and say_id not in keep_ids:
            # Retain the pending question by dropping the oldest retained row,
            # matching the in-RAM trim rule exactly.
            keep_ids.discard(rows[MAX_COMMANDS - 1][1])
            keep_ids.add(say_id)
        for rowid, command_id in rows:
            if command_id in keep_ids:
                continue
            self.db.execute('DELETE FROM commands WHERE rowid=?', (rowid,))
        if not self._atomic_depth:
            self.db.commit()

    def close(self):
        self.db.close()

    def emit(self, channel, data):
        if self._emitting_suspended:
            # Inside an open transaction: hold until the commit succeeds so no
            # subscriber observes uncommitted state.
            self._pending_emits.append((channel, data))
            return
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
                                 'memory': 'local_sqlite', 'voice': ('receipt_required' if self.require_audio_receipt else 'queued_only'),
                                 'vision': 'ingest_only'}}

    def events(self, since=0):
        return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM events WHERE ts > ? ORDER BY ts, id', (since,))]

    def event(self, kind, severity, evidence, ts=None, reason=None):
        event = Event(ts=self.clock() if ts is None else ts, event_id=uuid4(), kind=kind, severity=severity, evidence=evidence, reason=reason).model_dump(mode='json')
        self.db.execute('INSERT INTO events VALUES (?, ?, ?)', (event['event_id'], event['ts'], json.dumps(event)))
        if not self._atomic_depth:
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
            if not self._atomic_depth:
                self.db.commit()
            self.emit('event', event)
        return event

    def queue_command(self, data):
        item = {'command_id': str(uuid4()), 'ts': self.clock(), 'status': 'queued', 'seq': self.clock(), **data}
        self.commands.append(item)
        say_id = (self.pending or {}).get('say_command_id') if self.pending else None
        trimmed = self.commands[-MAX_COMMANDS:]
        if say_id and all(entry['command_id'] != say_id for entry in trimmed):
            # The pending check-in question must stay receivable, so it is
            # retained even when older than the bounded window.
            say_item = next(entry for entry in self.commands if entry['command_id'] == say_id)
            trimmed = [say_item] + trimmed[:-1]
        self.commands = trimmed
        self._save_command(item)
        self.emit('command', item)
        return item

    @atomic('command_receipt')
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
            self._save_command(item)
            self.emit('command', item)
            self.checkin_delivery(item)
        return item

    def checkin_delivery(self, item):
        # Queued, accepted, and executing receipts never count as audio delivery.
        pending = self.pending
        if not (self.require_audio_receipt and pending and pending.get('phase') == 'awaiting_playback'
                and item['cmd'] == 'say' and item['command_id'] == pending.get('say_command_id')):
            return
        now = self.clock()
        if item['status'] == 'completed':
            # Only a completed simulation receipt for this exact say command starts
            # the eight-second reassurance window.
            pending.update(phase='awaiting_reply', audio_started_at=now,
                           deadline_at=now + REPLY_WINDOW_MS, audio_deadline_at=None)
            self._save_pending()
            self.emit('checkin', pending)
        elif item['status'] == 'failed':
            self.audio_failed(now, 'playback_failed')

    def audio_failed(self, now, reason='playback_failed'):
        if self.pending:
            # Delivery failure: the check-in was never heard, so label it as such
            # instead of a no-reply reassurance timeout.
            self.event('checkin_audio_failed', 'warn', self.pending_evidence, reason=reason)
            self.escalate(now)

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
            map_changed = bool(self.perception) and self.perception['pose']['map_id'] != payload['map_id']
            if map_changed:
                # The old capture was observed on the previous map; showing it
                # under the new empty map would misplace the resident.
                # History/memory rows are retained for audit and retrieval.
                self.perception = None
                self.emit('brain.perception', None)
            if self.candidate_map != payload['map_id']:
                self.candidate = self.candidate_map = None
            self.rearm = []  # A map change invalidates cross-map recovery evidence.
            self.map = payload
        self.emit(channel, payload)
        return True

    @atomic('observe')
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
        # Best effort and inside this transaction: an unavailable embedder
        # returns False (bounded by its timeout, then skipped for a cooldown)
        # and never aborts the safety rules below.
        self.semantic.index(str(frame.frame_id), frame.pose.map_id, frame.ts, frame.caption)
        self.db.execute('DELETE FROM memory WHERE id NOT IN (SELECT id FROM memory ORDER BY ts DESC, rowid DESC LIMIT 1000)')
        self.last_ts, self.perception = frame.ts, payload
        self.emit('brain.perception', payload)
        risky = frame.person and frame.posture == 'lying' and frame.location in ('floor', 'chair') and frame.confidence >= .8
        safe = frame.confidence >= .8 and (not frame.person or (frame.posture != 'unknown' and frame.location != 'unknown' and not risky))
        if not risky:
            self.candidate = self.candidate_map = None
            if self.pending is None:
                if self.episode:
                    # Recovery after an episode needs two fresh, confident,
                    # affirmatively non-risky captures within five seconds,
                    # spaced at least 500 ms apart; a single empty frame is
                    # never evidence the resident recovered.
                    present_safe = frame.person and safe
                    if present_safe and frame.ts >= now - 5000:
                        self.rearm = [entry for entry in self.rearm if 0 <= frame.ts - entry <= 5000]
                        self.rearm.append(frame.ts)
                        if len(self.rearm) >= 2 and frame.ts - self.rearm[-2] >= 500:
                            self.episode = False
                            self.rearm = []
                            self._save_episode()
            return True
        self.rearm = []
        if self.episode:
            return True
        previous = self.candidate if self.candidate_map == frame.pose.map_id else None
        self.candidate, self.candidate_map = frame.ts, frame.pose.map_id
        gap = None if previous is None else frame.ts - previous
        if previous is None or gap > 5000 or (self.require_audio_receipt and gap < 250):
            # Receipt mode follows the acceptance profile: qualifying captures
            # are at least 250 ms apart. The legacy demo keeps its original
            # equal-timestamp behavior.
            return True
        self.episode = True
        evidence = {'frame_id': str(frame.frame_id), 'pose': payload['pose'], 'crop_url': None}
        event = self.event('fall_suspected', 'warn', evidence)
        self.pending = {'event_id': event['event_id'], 'started_at': now, 'deadline_at': now + REPLY_WINDOW_MS}
        command = self.queue_command({'text': 'Are you okay? Please say okay or help.', 'cmd': 'say'})
        if self.require_audio_receipt:
            # Queueing is not delivery: the reply window opens only after a
            # completed audio receipt, and playback itself has an 8s deadline.
            self.pending.update(phase='awaiting_playback', say_command_id=command['command_id'],
                                deadline_at=None, audio_deadline_at=now + AUDIO_WATCHDOG_MS, audio_started_at=None)
        else:
            # Default demo: timing starts at queue, delivery is unverified.
            self.pending.update(phase='queued_demo', delivery='unverified demo')
        self.pending_evidence = evidence
        self._save_pending()
        self._save_episode()
        self.emit('checkin', self.pending)
        return True

    @atomic('reply')
    def reply(self, voice: VoiceHeard):
        pending = self.pending
        if not pending or voice.confidence < .8 or str(voice.event_id) != pending['event_id']:
            return
        now = self.clock()
        text = re.sub(r'[^a-z ]', '', voice.text.lower()).strip()
        if text in ('help', 'help me', 'i need help'):
            # Explicit help is positive confirmation and can escalate even
            # before audio playback has completed.
            self.escalate(now)
            return
        if pending.get('phase') == 'awaiting_playback' or pending.get('deadline_at') is None:
            # Reassurance only counts once playback is verified and the window opened.
            return
        window_start = pending.get('audio_started_at') or pending['started_at']
        if not window_start <= voice.ts <= min(now, pending['deadline_at']) or now >= pending['deadline_at']:
            self.tick(now)
            return
        if text in ('okay', 'ok', 'im okay', 'i am okay', 'im ok', 'i am ok'):
            self.event('checkin_ok', 'info', self.pending_evidence)
            self.pending = None
            self._save_pending()
            self.emit('checkin', None)

    def escalate(self, now):
        if self.pending:
            self.event('fall_confirmed', 'critical', self.pending_evidence)
            self.pending = None
            self._save_pending()
            self.emit('checkin', None)

    @atomic('apply_voice_reply')
    def apply_voice_reply(self, intent, event_id, now):
        """Atomically apply one quality-accepted synthetic voice reply.

        Counts only for the exact incident while its reply window is open;
        the caller has already validated capture timing and Whisper quality.
        No confidence is fabricated; the emitted payloads are the same
        checkin_ok / fall_confirmed kinds the text path emits.
        """
        pending = self.pending
        if not pending or str(event_id) != str(pending.get('event_id')):
            return False
        if pending.get('phase') == 'awaiting_playback':
            return False
        deadline = pending.get('deadline_at')
        # A validated capture that completed by the deadline may arrive up to
        # RESULT_GRACE_MS late in wall time without losing its effect.
        if deadline is not None and now > deadline + audio_reply.RESULT_GRACE_MS:
            return False
        if intent == 'reassurance':
            self.event('checkin_ok', 'info', self.pending_evidence)
            self.pending = None
            self._save_pending()
            self.emit('checkin', None)
            return True
        if intent == 'concern':
            # Explicit concern escalates even while still awaiting playback,
            # mirroring the help path in reply(); this method itself only
            # rejects for a cleared or non-matching incident.
            self.escalate(now)
            return True
        return False

    @atomic('tick')
    def tick(self, now=None):
        now = self.clock() if now is None else now
        if not self.pending:
            return
        audio_deadline = self.pending.get('audio_deadline_at')
        if audio_deadline is not None and now >= audio_deadline:
            self.audio_failed(now, 'playback_timeout' if self.require_audio_receipt else 'playback_failed')
        elif self.pending.get('deadline_at') is not None and now >= self.pending['deadline_at']:
            grace_until = audio_reply.pending_grace(self)
            if grace_until is not None and now <= grace_until:
                return
            registry = getattr(self, '_audio_reply_registry', None) or {}
            entry = registry.get(str(self.pending.get('event_id')))
            if not self.require_audio_receipt:
                # Legacy demo path: the historical plain checkin_no_reply
                # stands; ambiguity or silence was never claimed.
                self.event('checkin_no_reply', 'warn', self.pending_evidence)
            elif entry is not None and entry['ended_at'] is None:
                # Registered in-flight capture past every bound: recognition
                # failed, distinct from resident silence.
                self.event('checkin_audio_failed', 'warn', self.pending_evidence,
                           reason='recognition_timeout')
            else:
                completed_rejected = entry is not None and entry['ended_at'] is not None
                if completed_rejected:
                    # Input existed, completed in valid bounds, but the
                    # transcript was ambiguous or failed quality: not
                    # evidence about the resident, so plain no-reply.
                    self.event('checkin_no_reply', 'warn', self.pending_evidence)
                else:
                    # Receipt mode with absent input: the capture pipeline
                    # never produced audio; the honest label is audio failure.
                    self.event('checkin_audio_failed', 'warn', self.pending_evidence,
                               reason='input_unavailable')
            # `now` can be an explicit demo deadline; event timestamps remain wall time.
            self.escalate(now)

    def query(self, text):
        from .episodic_memory import EpisodicMemory
        map_data = self.map or {}
        return EpisodicMemory(self.db, now=self.clock, map_id=map_data.get('map_id'),
                              waypoints=map_data.get('waypoints', ())).ask(text)

    def recall(self, goal, map_id, ts_to=None, limit=6):
        now = self.clock()
        # Future-dated frames are never evidence, whatever bound the caller asks for.
        ts_to = now if ts_to is None else min(ts_to, now)
        return self.semantic.recall(goal, map_id=map_id, ts_to=ts_to, limit=limit)

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
                deadline = self.pending.get('deadline_at')
                self.tick(self.pending.get('audio_deadline_at') if deadline is None else deadline)
        elif name == 'safe_bed':
            # Deliberate operator reset: clears a resolved episode without an
            # active check-in and ends the re-arming requirement for the demo.
            if self.pending is None:
                self.episode = False
                self.rearm = []
                self._save_episode()
            for ts in [now]:
                self.ingest('brain.perception', {'ts': ts, 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying',
                    'location': 'bed', 'confidence': .95,
                    'caption': 'Demo: resident resting on the bed; glasses on the bedside table.',
                    'pose': {'x': 6, 'y': 2}})
        elif name == 'fall' and self.require_audio_receipt:
            # Acceptance profile: qualifying captures are distinct
            # acquisitions at least 250 ms apart, so receipt mode queues one
            # per call; the driver advances the clock before the second.
            self.ingest('brain.perception', {'ts': now, 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying',
                'location': 'floor', 'confidence': .95,
                'caption': 'Demo: resident lying on the floor beside the blue mug.',
                'pose': {'x': 6, 'y': 2}})
        else:
            for ts in ([now, now] if name == 'fall' else [now]):
                self.ingest('brain.perception', {'ts': ts, 'frame_id': str(uuid4()), 'person': True, 'posture': 'lying',
                    'location': 'floor' if name == 'fall' else 'bed', 'confidence': .95,
                    'caption': 'Demo: resident lying on the floor beside the blue mug.' if name == 'fall' else 'Demo: resident resting on the bed; glasses on the bedside table.',
                    'pose': {'x': 6, 'y': 2}})
