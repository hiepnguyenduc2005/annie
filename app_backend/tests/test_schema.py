"""Household schema: profiles, messages, reminders, history, emergencies.

These run against the in-memory fallback so they need no database. The
MongoDB path is covered separately in test_schema_mongo.py, which skips when
no server is listening.
"""
from fastapi.testclient import TestClient

from app.family import FamilyService
from app.main import create_app

SECRET = 's3cret'


def make_client():
    # mock=True keeps dispatch off the network; these tests are about storage.
    return TestClient(create_app(':memory:', token='', internal_secret=SECRET,
                                 family_service=FamilyService(mock=True, mock_speed=0)))


def internal(client, path, payload):
    return client.post(path, headers={'X-Internal-Secret': SECRET}, json=payload)


def test_storage_reports_the_in_memory_fallback_and_why():
    with make_client() as client:
        status = client.get('/api/storage').json()
        assert status['backend'] == 'memory'
        assert status['reason'] == 'MONGODB_URI is not set'


def test_app_users_are_many_to_one_onto_dog_users():
    with make_client() as client:
        grandma = client.post('/api/dog-users', json={'name': 'grandma'}).json()
        memaw = client.post('/api/dog-users', json={'name': 'memaw'}).json()

        a = client.post('/api/app-users', json={'name': 'a', 'dog_user_id': grandma['id']}).json()
        b = client.post('/api/app-users', json={'name': 'b', 'dog_user_id': grandma['id']}).json()
        c = client.post('/api/app-users', json={'name': 'c', 'dog_user_id': memaw['id']}).json()

        assert [a['id'], b['id'], c['id']] == [1, 2, 3]
        # Two family members share one resident, as in the schema.
        assert a['dog_user_id'] == b['dog_user_id'] == grandma['id']
        assert len(client.get('/api/app-users', params={'dog_user_id': grandma['id']}).json()) == 2
        assert len(client.get('/api/app-users', params={'dog_user_id': memaw['id']}).json()) == 1


def test_app_user_needs_a_real_dog_user():
    with make_client() as client:
        assert client.post('/api/app-users', json={'name': 'x', 'dog_user_id': 'nope'}).status_code == 422


def test_dog_user_id_can_be_supplied_and_must_be_unique():
    with make_client() as client:
        assert client.post('/api/dog-users', json={'name': 'grandma', 'id': 'xxx'}).json()['id'] == 'xxx'
        assert client.post('/api/dog-users', json={'name': 'other', 'id': 'xxx'}).status_code == 409


def test_message_accumulates_the_robot_reply():
    with make_client() as client:
        dog = client.post('/api/dog-users', json={'name': 'grandma'}).json()
        user = client.post('/api/app-users', json={'name': 'zach', 'dog_user_id': dog['id']}).json()

        message = client.post('/api/schema/messages', json={
            'dog_user_id': dog['id'], 'app_user_id': user['id'],
            'text': 'Please remind her to plug in her phone.'}).json()
        assert [t['from'] for t in message['texts']] == ['app']

        # Yellow path: robot_backend reports back.
        reply = internal(client, '/internal/message-reply', {
            'message_id': message['id'],
            'text': 'Told her; she said she forgot where it was.'})
        assert reply.status_code == 202

        stored = client.get('/api/schema/messages', params={'dog_user_id': dog['id']}).json()
        assert len(stored) == 1
        assert [t['from'] for t in stored[0]['texts']] == ['app', 'robot']
        assert 'forgot' in stored[0]['texts'][1]['text']


def test_message_reply_for_unknown_message_is_404():
    with make_client() as client:
        assert internal(client, '/internal/message-reply',
                        {'message_id': 'nope', 'text': 'x'}).status_code == 404


def test_reminder_history_records_what_actually_happened():
    with make_client() as client:
        dog = client.post('/api/dog-users', json={'name': 'grandma'}).json()
        reminder = client.post('/api/schema/reminders', json={
            'dog_user_id': dog['id'], 'hour': 8, 'item': 'Take morning pills'}).json()

        assert client.get(f'/api/schema/reminders/{reminder["id"]}/history').json() == []

        # Red path: robot_backend reports the actual activity.
        assert internal(client, '/internal/reminder-history', {
            'reminder_id': reminder['id'],
            'description': 'grandma took her pills'}).status_code == 202

        history = client.get(f'/api/schema/reminders/{reminder["id"]}/history').json()
        assert len(history) == 1
        assert history[0]['description'] == 'grandma took her pills'
        assert history[0]['reminder_id'] == reminder['id']
        assert history[0]['timedate'] > 0


def test_history_for_unknown_reminder_is_404_both_ways():
    with make_client() as client:
        assert client.get('/api/schema/reminders/nope/history').status_code == 404
        assert internal(client, '/internal/reminder-history',
                        {'reminder_id': 'nope', 'description': 'x'}).status_code == 404


def test_emergencies_are_received_from_the_robot_and_listed_newest_first():
    with make_client() as client:
        dog = client.post('/api/dog-users', json={'name': 'grandma'}).json()

        # Cyan path.
        assert internal(client, '/internal/emergencies', {
            'dog_user_id': dog['id'], 'description': 'No response for two minutes',
            'timestamp': 1000}).status_code == 202
        assert internal(client, '/internal/emergencies', {
            'dog_user_id': dog['id'], 'description': 'Resident asked for help',
            'timestamp': 2000}).status_code == 202

        emergencies = client.get('/api/schema/emergencies').json()
        assert [e['timestamp'] for e in emergencies] == [2000, 1000]
        assert emergencies[0]['description'] == 'Resident asked for help'


def test_emergency_for_unknown_dog_user_is_rejected():
    with make_client() as client:
        assert internal(client, '/internal/emergencies',
                        {'dog_user_id': 'nope', 'description': 'x'}).status_code == 422


def test_all_three_inbound_routes_require_the_shared_secret():
    with make_client() as client:
        for path, payload in (
            ('/internal/message-reply', {'message_id': 'a', 'text': 'x'}),
            ('/internal/reminder-history', {'reminder_id': 'a', 'description': 'x'}),
            ('/internal/emergencies', {'dog_user_id': 'a', 'description': 'x'}),
        ):
            assert client.post(path, json=payload).status_code == 401
            assert client.post(path, headers={'X-Internal-Secret': 'wrong'},
                               json=payload).status_code == 401


def test_schema_routes_do_not_disturb_the_existing_run_flow():
    with make_client() as client:
        run = client.post('/api/messages', json={'author_id': 'zach', 'text': 'hi'})
        assert run.status_code == 202
        assert run.json()['status'] == 'dispatched'
        assert len(client.get('/api/thread').json()) == 1
