"""In-memory companion state for the resident-facing view: reminders, the
observation feed, and lexical recall over it.

Wire shapes match the Swift companion client in app_frontend exactly
(`/api/reminders`, `/api/memory`, `/api/ask`), so one app_backend serves both
the resident and family views instead of a second server on a second address.

The same fact store backs the family run's `recalled` event, so what the dog
says it remembers is what the resident's activity feed shows.
"""
import re
from datetime import datetime, timedelta
from pydantic import Field

from .models import StrictModel, Text


class NewReminder(StrictModel):
    time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    title: Text


RESIDENT = 'Jeanine'  # the person Annie looks after; the family app says grandma


class AskRequest(StrictModel):
    question: Text


def _iso(when):
    return when.strftime('%Y-%m-%dT%H:%M:%S')


def seed_reminders():
    return [
        {'id': 1, 'time': '08:00', 'title': 'Take morning medication', 'done': True},
        {'id': 2, 'time': '09:30', 'title': 'Morning walk with Annie', 'done': True},
        {'id': 3, 'time': '12:30', 'title': 'Take midday medication', 'done': False},
        {'id': 4, 'time': '15:00', 'title': 'Charge your phone', 'done': False},
        {'id': 5, 'time': '18:00', 'title': 'Take evening medication', 'done': False},
    ]


def seed_memory(now=None):
    now = now or datetime.now()
    yesterday = now - timedelta(days=1)
    return [
        {'id': 1, 'subject': 'user', 'relation': 'took', 'object': 'morning_medication', 'room': 'kitchen',
         'timestamp': _iso(now.replace(hour=8, minute=2, second=0, microsecond=0)),
         'text': 'Saw you take your morning medication in the kitchen.'},
        {'id': 2, 'subject': 'phone', 'relation': 'located_at', 'object': 'living_room_couch', 'room': 'living_room',
         'timestamp': _iso(yesterday.replace(hour=15, minute=20, second=0, microsecond=0)),
         'text': 'Your phone was on the living room couch, by the left cushion.'},
        {'id': 3, 'subject': 'user', 'relation': 'sat_on', 'object': 'living_room_couch', 'room': 'living_room',
         'timestamp': _iso(yesterday.replace(hour=15, minute=40, second=0, microsecond=0)),
         'text': 'You were reading on the living room couch yesterday afternoon.'},
        {'id': 4, 'subject': 'glasses', 'relation': 'located_at', 'object': 'kitchen_table', 'room': 'kitchen',
         'timestamp': _iso(now.replace(hour=9, minute=15, second=0, microsecond=0)),
         'text': 'Glasses last seen on the kitchen table.'},
        {'id': 5, 'subject': 'dog', 'relation': 'followed', 'object': 'user', 'room': 'living_room',
         'timestamp': _iso(now.replace(hour=11, minute=40, second=0, microsecond=0)),
         'text': 'Followed you to the living room.'},
    ]


# The fact the dog cites in the family run's `recalled` event.
PHONE_FACT_ID = 2

STOP_WORDS = {'where', 'when', 'was', 'were', 'the', 'a', 'an', 'is', 'did', 'i', 'my', 'you',
              'see', 'last', 'what', 'me', 'for', 'of', 'in', 'at', 'to', 'do', 'have', 'it'}


class CompanionService:
    def __init__(self, now=None):
        self.reminders = seed_reminders()
        self.alerts = []
        self.live_facts = []
        self.memory = seed_memory(now)

    def add_reminder(self, time, title):
        item = {'id': max((r['id'] for r in self.reminders), default=0) + 1,
                'time': time, 'title': title, 'done': False}
        self.reminders.append(item)
        self.reminders.sort(key=lambda r: r['time'])
        return item

    def toggle_reminder(self, reminder_id):
        item = next((r for r in self.reminders if r['id'] == reminder_id), None)
        if item is None:
            raise KeyError(reminder_id)
        item['done'] = not item['done']
        return item

    def phone_fact(self):
        return next((f for f in self.memory if f['id'] == PHONE_FACT_ID), None)

    # ---- live facts from the dog process (its space-time graph), merged ahead of the seeded ones ----
    live_facts: list = []
    alerts: list = []  # emergencies from missions (see main._apply_outcome); newest last, bounded

    @staticmethod
    def family_wording(sentence: str) -> str:
        """The dog's memory sentences are written for its planner; trim the technical asides for the family."""
        t = re.sub(r"\s*\((?:\d+ tracker id\(s\)|ids churn)[^)]*\)", "", sentence)
        t = re.sub(r",?\s*\d+ occupied voxels remembered", "", t)
        t = t.replace("an unidentified person", "someone").replace("An unidentified person", "Someone")
        t = re.sub(r"\bplace-(\d+)\b", r"spot \1", t)
        t = re.sub(r"\bthe dog\b", "Annie", t)
        return t.strip()

    def merge_live(self, telemetry: dict, now=None) -> int:
        """Turn the dog process' /telemetry.json into memory facts: what it remembers (graph sentences), who it
        greeted, check-ins, instructions. Replaces the previous live batch; ids are negative so they never
        collide with the seeded/added facts. Returns how many facts came in."""
        now = now or datetime.now()
        facts = []
        base = 10_000
        for i, raw in enumerate(list((telemetry or {}).get('graph_sentences') or [])[:8]):
            sentence = self.family_wording(raw)
            if not sentence:
                continue
            facts.append({'id': -(base + i), 'subject': 'annie', 'relation': 'remembers', 'object': 'scene', 'room': 'home',
                          'timestamp': _iso(now), 'text': sentence[0].upper() + sentence[1:] + ('.' if not sentence.endswith('.') else '')})
        state = (telemetry or {}).get('state') or {}
        t_now = float(state.get('t_s') or 0.0)
        for j, g in enumerate(list((telemetry or {}).get('greetings') or [])[-6:]):
            ago = max(0.0, t_now - float(g.get('t_s') or 0.0))
            who = g.get('name') or 'someone'
            facts.append({'id': -(base + 100 + j), 'subject': 'annie', 'relation': 'greeted', 'object': who, 'room': 'home',
                          'timestamp': _iso(now - timedelta(seconds=ago)), 'text': f"Said hello to {who}: \"{g.get('text') or ''}\""})
        for j, c in enumerate(list((telemetry or {}).get('checkins') or [])[-4:]):
            ago = max(0.0, t_now - float(c.get('t_s') or 0.0))
            facts.append({'id': -(base + 200 + j), 'subject': 'annie', 'relation': 'checked_on', 'object': 'person', 'room': 'home',
                          'timestamp': _iso(now - timedelta(seconds=ago)), 'text': 'Someone was lying down; asked if they were alright.'})
        for j, ins in enumerate(list((telemetry or {}).get('instructions') or [])[-4:]):
            ago = max(0.0, t_now - float(ins.get('t_s') or 0.0))
            facts.append({'id': -(base + 300 + j), 'subject': 'family', 'relation': 'asked', 'object': 'annie', 'room': 'home',
                          'timestamp': _iso(now - timedelta(seconds=ago)),
                          'text': f"Asked: \"{ins.get('text') or ''}\" -> {ins.get('reply') or ins.get('source') or 'done'}"})
        self.live_facts = sorted(facts, key=lambda f: f['timestamp'])
        return len(facts)

    def all_memory(self):
        return self.memory + self.live_facts

    RESIDENT_ALIASES = ('grandma', 'grandmother', 'granny', 'nana', 'gran', 'she', 'her', 'mum', 'mom', 'mother')

    def ask(self, question):
        # Lexical recall over recorded observations (live ones from the dog first); no model call, and no
        # answer invented when nothing matches. The family says "grandma"/"she"; the dog's memory says the name.
        words = set(re.findall(r'\w+', question.lower())) - STOP_WORDS
        if words & set(self.RESIDENT_ALIASES):
            words = (words - set(self.RESIDENT_ALIASES)) | {RESIDENT.lower()}
        best, best_score = None, 0
        for fact in reversed(self.all_memory()):
            haystack = set(re.findall(r'\w+', (fact['text'] + ' ' + fact['subject'] + ' ' + fact['object']).lower()))
            score = len(words & haystack)
            if score > best_score:
                best, best_score = fact, score
        if best is None:
            if RESIDENT.lower() in words:  # asked about her, no sighting by name: say what was seen, honestly
                seen = next((f for f in reversed(self.live_facts) if 'someone' in f['text'].lower() and 'seen' in f['text'].lower()), None)
                if seen:
                    return f"I haven't recognised {RESIDENT} by name yet. {seen['text']}"
                return f"I haven't seen {RESIDENT} yet today, but I'm keeping watch."
            return "I don't have anything on that yet, but I'm keeping watch."
        return best['text']
