import time

from fastapi.testclient import TestClient

from app.family import FamilyService
from app.main import create_app


def make_client(**kwargs):
    return TestClient(create_app(':memory:', token='', **kwargs))


def test_reminders_list_add_and_toggle():
    with make_client() as client:
        reminders = client.get('/api/reminders').json()
        assert reminders and all({'id', 'time', 'title', 'done'} == set(r) for r in reminders)

        created = client.post('/api/reminders', json={'time': '16:30', 'title': 'Call Zach back'})
        assert created.status_code == 200
        assert created.json()['done'] is False
        new_id = created.json()['id']

        toggled = client.patch(f'/api/reminders/{new_id}/toggle').json()
        assert toggled['done'] is True
        assert client.patch(f'/api/reminders/{new_id}/toggle').json()['done'] is False
        assert client.patch('/api/reminders/9999/toggle').status_code == 404


def test_reminder_time_must_be_hh_mm():
    with make_client() as client:
        assert client.post('/api/reminders', json={'time': 'later', 'title': 'x'}).status_code == 422
        assert client.post('/api/reminders', json={'time': '25:00', 'title': 'x'}).status_code == 422


def test_memory_feed_includes_the_phone_observation():
    with make_client() as client:
        memory = client.get('/api/memory').json()
        phones = [fact for fact in memory if fact['subject'] == 'phone']
        assert len(phones) == 1
        assert 'couch' in phones[0]['text'].lower()


def test_ask_recalls_from_observations_and_refuses_otherwise():
    with make_client() as client:
        phone = client.post('/api/ask', json={'question': 'Where is my phone?'}).json()['answer']
        assert 'couch' in phone.lower()
        glasses = client.post('/api/ask', json={'question': 'Where are my glasses?'}).json()['answer']
        assert 'kitchen table' in glasses.lower()
        unknown = client.post('/api/ask', json={'question': 'Did an elephant visit?'}).json()['answer']
        assert unknown == "I don't have anything on that yet, but I'm keeping watch."


def test_mock_run_cites_the_same_phone_observation_the_feed_shows():
    family = FamilyService(mock=True, mock_speed=0.01)
    with make_client(family_service=family) as client:
        feed_fact = next(f for f in client.get('/api/memory').json() if f['subject'] == 'phone')
        run_id = client.post('/api/messages', json={
            'author_id': 'zach',
            'text': 'I think Grandma\'s phone is dead, could you remind her to plug it in?',
        }).json()['run_id']

        deadline = time.monotonic() + 5
        run = client.get(f'/api/runs/{run_id}').json()
        while run['status'] != 'completed' and time.monotonic() < deadline:
            time.sleep(0.02)
            run = client.get(f'/api/runs/{run_id}').json()

        assert run['status'] == 'completed'
        kinds = [event['kind'] for event in run['events']]
        assert kinds == ['navigating', 'arrived', 'speaking', 'listening', 'heard',
                         'recalling', 'recalled', 'speaking', 'completed']
        recalled = next(e for e in run['events'] if e['kind'] == 'recalled')
        assert recalled['payload']['note'] == feed_fact['text']
        # The relayed line quotes the sender rather than inventing a paraphrase.
        relay = run['events'][2]['payload']['text']
        assert 'Zach' in relay and 'plug it in' in relay
