import asyncio
import base64
import io
import json
import unittest
import wave
from types import SimpleNamespace

from robot_backend.app.config import Settings
from robot_backend.app.schemas import RemoteRequest
from robot_backend.app.sessions.manager import InMemorySessionManager
from robot_backend.app.services.live_audio import AudioHub, AudioConversation


def wav():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b'\x00\x00' * 320)
    return base64.b64encode(stream.getvalue()).decode()


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.events = asyncio.Queue()

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, event):
        await self.events.put(event)
        if event['type'] == 'audio_end':
            await self.control(type='playback_finished', turn_id=event['turn_id'])

    async def send_bytes(self, data):
        pass

    async def control(self, **body):
        await self.incoming.put({'type': 'websocket.receive', 'text': json.dumps(body)})

    async def next(self, kind):
        while True:
            event = await asyncio.wait_for(self.events.get(), 2)
            if event['type'] == kind:
                return event


class Agent:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.calls = []

    async def generate(self, session_id, content, start=False, sample_rate=16000):
        self.calls.append((content, start))
        return SimpleNamespace(audio=wav(), done=len(self.calls) == 2)


class Speech:
    def __init__(self):
        self.calls = 0

    async def listen(self, packets, ready, transcript):
        self.calls += 1
        await ready()
        return 'I am doing well'


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def setup_conversation(self):
        settings = Settings(_env_file=None)
        sessions = InMemorySessionManager(settings)
        session = await sessions.create(RemoteRequest(request_id='remote-1', type='checkin', request='Ask how Jeanine feels'))
        hub = AudioHub()
        hub.offer(session.session_id)
        socket, speech = Socket(), Speech()
        agent = Agent(settings, sessions)
        conversation = AudioConversation(socket, agent, speech, hub)
        return session, hub, socket, speech, agent, conversation

    async def test_idle_phone_is_invited_without_consuming_task(self):
        session, hub, socket, speech, agent, conversation = await self.setup_conversation()
        task = asyncio.create_task(conversation.run())
        try:
            invitation = await socket.next('conversation_requested')
            self.assertEqual(invitation['session_id'], session.session_id)
            self.assertEqual(list(hub.pending), [session.session_id])
            self.assertEqual(agent.calls, [])
            self.assertEqual(speech.calls, 0)
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
        # The next connected phone gets the still-unaccepted task.
        next_socket = Socket()
        next_conversation = AudioConversation(next_socket, agent, speech, hub)
        task = asyncio.create_task(next_conversation.run())
        try:
            self.assertEqual((await next_socket.next('conversation_requested'))['session_id'], session.session_id)
        finally:
            await next_socket.incoming.put({'type': 'websocket.disconnect'})
            await task

    async def test_proactive_opening_then_listen_then_stop(self):
        session, hub, socket, speech, agent, conversation = await self.setup_conversation()
        task = asyncio.create_task(conversation.run())
        try:
            await socket.next('conversation_requested')
            await socket.control(type='start', session_id=session.session_id)
            await socket.next('session_ended')
            stopped = await socket.next('state')
            self.assertEqual(stopped['state'], 'stopped')
            self.assertEqual(agent.calls[0], ([], True))
            self.assertEqual(agent.calls[1], ([{'type': 'text', 'text': 'I am doing well'}], False))
            self.assertEqual(speech.calls, 1)
            self.assertEqual(list(hub.pending), [])
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task


    async def test_stop_does_not_wait_for_callback_delivery(self):
        session, hub, socket, speech, agent, conversation = await self.setup_conversation()
        class BlockedSink:
            async def deliver_pending(self):
                raise AssertionError('Audio receive loop must not deliver callbacks')
        agent.sink = BlockedSink()
        task = asyncio.create_task(conversation.run())
        try:
            await socket.next('ready')
            await socket.control(type='stop')
            await socket.next('state')
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


    async def test_disconnect_before_ready_preserves_pending_task(self):
        from starlette.websockets import WebSocketDisconnect
        session, hub, socket, speech, agent, conversation = await self.setup_conversation()
        async def disconnected(event):
            raise WebSocketDisconnect(code=1006)
        socket.send_json = disconnected
        await conversation.run()
        self.assertEqual(list(hub.pending), [session.session_id])
        self.assertIsNone(conversation.worker)
        self.assertEqual(agent.calls, [])
