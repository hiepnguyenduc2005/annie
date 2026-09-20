"""The same schema against a real MongoDB.

Skips when nothing is listening, so a checkout without a database still runs
the suite. Uses its own database and drops it afterwards, so it never touches
demo data.
"""
import os
import socket
import uuid

import pytest
from fastapi.testclient import TestClient

from app.family import FamilyService
from app.main import create_app
from app.schema_store import SchemaStore

URI = os.getenv('MONGODB_TEST_URI', 'mongodb://127.0.0.1:27017')
SECRET = 's3cret'


def mongod_listening():
    host, _, port = URI.split('://', 1)[1].partition(':')
    try:
        with socket.create_connection((host or '127.0.0.1', int(port or 27017)), timeout=1):
            return True
    except (OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(not mongod_listening(), reason='no mongod on 127.0.0.1:27017')


@pytest.fixture
def mongo_client():
    db_name = f'annie_test_{uuid.uuid4().hex[:8]}'
    store = SchemaStore(uri=URI, db_name=db_name)
    app = create_app(':memory:', token='', internal_secret=SECRET, schema_store=store,
                     family_service=FamilyService(mock=True, mock_speed=0))
    with TestClient(app) as client:
        yield client
    # Drop the scratch database with a fresh client; the app closed its own.
    from pymongo import MongoClient
    MongoClient(URI).drop_database(db_name)


def test_records_persist_to_mongodb(mongo_client):
    assert mongo_client.get('/api/storage').json()['backend'] == 'mongodb'

    dog = mongo_client.post('/api/dog-users', json={'name': 'grandma'}).json()
    user = mongo_client.post('/api/app-users', json={'name': 'zach', 'dog_user_id': dog['id']}).json()
    message = mongo_client.post('/api/schema/messages', json={
        'dog_user_id': dog['id'], 'app_user_id': user['id'], 'text': 'plug in your phone'}).json()

    mongo_client.post('/internal/message-reply', headers={'X-Internal-Secret': SECRET},
                      json={'message_id': message['id'], 'text': 'she forgot where it was'})

    stored = mongo_client.get('/api/schema/messages').json()
    assert len(stored) == 1
    assert [t['from'] for t in stored[0]['texts']] == ['app', 'robot']
    # Mongo's own _id must not leak into the API surface.
    assert '_id' not in stored[0]


def test_reminder_history_and_emergencies_persist(mongo_client):
    dog = mongo_client.post('/api/dog-users', json={'name': 'grandma'}).json()
    reminder = mongo_client.post('/api/schema/reminders', json={
        'dog_user_id': dog['id'], 'hour': 8, 'item': 'pills'}).json()

    headers = {'X-Internal-Secret': SECRET}
    mongo_client.post('/internal/reminder-history', headers=headers,
                      json={'reminder_id': reminder['id'], 'description': 'grandma took her pills'})
    mongo_client.post('/internal/emergencies', headers=headers,
                      json={'dog_user_id': dog['id'], 'description': 'asked for help'})

    history = mongo_client.get(f'/api/schema/reminders/{reminder["id"]}/history').json()
    assert [h['description'] for h in history] == ['grandma took her pills']
    emergencies = mongo_client.get('/api/schema/emergencies').json()
    assert [e['description'] for e in emergencies] == ['asked for help']
