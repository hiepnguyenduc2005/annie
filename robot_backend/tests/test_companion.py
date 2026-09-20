import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import httpx
from fastapi import HTTPException
from robot_backend.app.config import Settings
from robot_backend.app.api.companion import MessageRequest, ReminderRequest
from robot_backend.app.services.companion import Companion
from robot_backend.app.schemas import FinalEvent

class Legacy:
    async def deliver_pending(self): pass
    async def enqueue(self, event): raise AssertionError('Wrong sink')

class Agent:
    def __init__(self): self.calls = 0
    async def request(self, body):
        self.calls += 1
        return SimpleNamespace(session_id='s'+str(self.calls))

class Tests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_correlation_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            requests = []
            def send(request):
                requests.append(request)
                return httpx.Response(503 if len(requests)==1 else 201)
            async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
                settings = Settings(_env_file=None, outbox_path=Path(directory)/'outbox', internal_secret='synthetic')
                adapter = Companion(settings, client, Legacy())
                agent, offered = Agent(), []
                hub = SimpleNamespace(offer=offered.append)
                body = MessageRequest(request_id=100,conversation_id=101,message_id=102,app_user_id=2,dog_user_id=1,day='2026-09-20',text='Check my grandma')
                await adapter.accept(body,'message',agent,hub)
                await adapter.accept(body,'message',agent,hub)
                self.assertEqual(agent.calls,1)
                with self.assertRaises(HTTPException):
                    await adapter.accept(body.model_copy(update={'text':'different'}),'message',agent,hub)
                await adapter.enqueue(FinalEvent(session_id='s1',request_id='companion-100',type='message',status='completed',summary='Resident confirmed she is comfortable.'))
                await adapter.deliver_pending()
                adapter.db.execute('UPDATE requests SET due=0'); adapter.db.commit()
                await adapter.deliver_pending()
                self.assertEqual(requests[0].content, requests[1].content)
                self.assertEqual(requests[1].headers['X-Internal-Secret'],'synthetic')
                self.assertIn(b'"reply_to":102',requests[1].content)
                reminder = ReminderRequest(request_id=200,occurrence_id=200,dog_user_id=1,reminder_id=4,day='2026-09-20',daily_time='08:00',scheduled_at='2026-09-20T12:00:00Z',description='Drink water')
                await adapter.accept(reminder,'reminder',agent,hub)
                adapter.close()
                adapter = Companion(settings,client,Legacy())
                await adapter.accept(reminder,'reminder',agent,hub)
                self.assertEqual(agent.calls,2)
                await adapter.deliver_pending()
                self.assertIn(b'"outcome":"unknown"',requests[-1].content)
                self.assertEqual(requests[-1].url.path,'/api/notes')
                adapter.close()

class EndpointTests(unittest.TestCase):
    def test_authentication_and_duplicate_endpoint(self):
        from fastapi.testclient import TestClient
        from robot_backend.app.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, outbox_path=Path(directory)/'outbox', internal_secret='synthetic')
            with TestClient(create_app(settings, sink=Legacy())) as client:
                body = dict(request_id=300,conversation_id=301,message_id=302,app_user_id=2,dog_user_id=1,day='2026-09-20',text='Check my grandma')
                self.assertEqual(client.post('/api/message-requests',json=body).status_code,401)
                headers={'X-Internal-Secret':'synthetic'}
                response=client.post('/api/message-requests',json=body,headers=headers)
                self.assertEqual(response.status_code,202)
                self.assertEqual(response.json(),{'request_id':300,'status':'accepted'})
                self.assertEqual(client.post('/api/message-requests',json=body,headers=headers).json(),response.json())
                self.assertEqual(len(client.app.state.audio_hub.pending),1)
                body['text']='Changed'
                self.assertEqual(client.post('/api/message-requests',json=body,headers=headers).status_code,409)
                body.update(request_id=400,dog_user_id=9)
                self.assertEqual(client.post('/api/message-requests',json=body,headers=headers).status_code,409)
