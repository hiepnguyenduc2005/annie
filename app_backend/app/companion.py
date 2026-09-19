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

    def ask(self, question):
        # Lexical recall over recorded observations only; no model call, and no
        # answer invented when nothing matches.
        words = set(re.findall(r'\w+', question.lower())) - STOP_WORDS
        best, best_score = None, 0
        for fact in reversed(self.memory):
            haystack = set(re.findall(r'\w+', (fact['text'] + ' ' + fact['subject'] + ' ' + fact['object']).lower()))
            score = len(words & haystack)
            if score > best_score:
                best, best_score = fact, score
        if best is None:
            return "I don't have anything on that yet, but I'm keeping watch."
        return best['text']
