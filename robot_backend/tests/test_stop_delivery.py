import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from robot_backend.app.config import Settings
from robot_backend.app.services.agent import Agent
from robot_backend.app.services.dedicated_server import DedicatedServer
from robot_backend.app.services.live_audio import AudioConversation, AudioHub
from robot_backend.app.services.qwen import QwenError
from robot_backend.app.sessions.manager import InMemorySessionManager


class FailingModel:
    async def structured(self, *args):
        raise QwenError('Qwen returned invalid internal analysis')


class Socket:
    def __init__(self):
        self.events = []
        self.messages = iter([
            {'type': 'websocket.receive', 'text': '{"type":"stop"}'},
            {'type': 'websocket.receive', 'text': '{"type":"stop"}'},
            {'type': 'websocket.disconnect'},
        ])

    async def receive(self):
        return next(self.messages)

    async def send_json(self, event):
        self.events.append(event)


class StopDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, configured=True, status=200, enabled=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings(_env_file=None, final_result_delivery_enabled=enabled,
            dedicated_server_url='http://127.0.0.1:8000/results' if configured else '',
            outbox_path=Path(directory.name) / 'outbox.db')
        requests = []

        async def handler(request):
            requests.append(request)
            await asyncio.sleep(0)
            return httpx.Response(status)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        sink = DedicatedServer(settings, client)
        self.addCleanup(sink.close)
        sessions = InMemorySessionManager(settings)
        session = await sessions.create()
        session.history = [{'role': 'user', 'content': 'synthetic private detail'}]
        agent = Agent(settings, sessions, FailingModel(), sink, None)
        socket = Socket()
        conversation = AudioConversation(socket, agent, None, AudioHub())
        conversation.session_id = session.session_id
        await conversation.run()
        self.assertEqual(requests, [])  # Socket processing never waits on HTTP.
        await sink.deliver_pending()  # Simulate the periodic outbox worker.
        return sink, session, socket, requests

    async def test_stop_posts_once_even_when_analysis_fails(self):
        sink, session, socket, requests = await self.exercise()
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload['status'], 'inconclusive')
        self.assertEqual(requests[0].headers['Idempotency-Key'], session.session_id)
        self.assertNotIn('synthetic private detail', requests[0].content.decode())
        self.assertEqual(session.history, [])
        self.assertIsNotNone(sink.db.execute('SELECT delivered_at FROM outbox').fetchone()[0])

    async def test_unconfigured_destination_is_visible_and_retains_result(self):
        sink, _, socket, requests = await self.exercise(configured=False)
        self.assertEqual(requests, [])
        self.assertIsNotNone(sink.db.execute('SELECT payload FROM outbox').fetchone()[0])

    async def test_failed_api_retains_result_and_concurrent_retry_posts_once(self):
        sink, _, _, requests = await self.exercise(status=503)
        self.assertEqual(len(requests), 1)
        self.assertEqual(sink.db.execute('SELECT attempts FROM outbox').fetchone()[0], 1)
        sink.db.execute('UPDATE outbox SET next_attempt=0')
        sink.db.commit()
        await asyncio.gather(sink.deliver_pending(), sink.deliver_pending())
        self.assertEqual(len(requests), 2)
        self.assertIsNotNone(sink.db.execute('SELECT payload FROM outbox').fetchone()[0])

    async def test_disabled_delivery_keeps_summary_without_calling_configured_api(self):
        sink, session, _, requests = await self.exercise(enabled=False)
        self.assertEqual(requests, [])
        self.assertEqual(session.status, 'ended')
        self.assertIsNotNone(sink.db.execute('SELECT payload FROM outbox').fetchone()[0])

    async def test_audio_service_failure_logs_safe_reason(self):
        from robot_backend.app.services.deepgram import SpeechError

        class FailedLive:
            async def listen(self, packets, ready, transcript):
                raise SpeechError('Set DEEPGRAM_API_KEY to enable speech', 503)

        settings = Settings(_env_file=None)
        sessions = InMemorySessionManager(settings)
        agent = Agent(settings, sessions, FailingModel(), None, None)
        socket = Socket()
        conversation = AudioConversation(socket, agent, FailedLive(), AudioHub())
        with self.assertLogs('robot_backend.app.services.live_audio', level='WARNING') as logs:
            await conversation.converse()
        self.assertIn('Set DEEPGRAM_API_KEY', logs.output[0])
        self.assertIn({'type': 'state', 'state': 'stopped'}, socket.events)
        self.assertTrue(any(event['type'] == 'error' for event in socket.events))
