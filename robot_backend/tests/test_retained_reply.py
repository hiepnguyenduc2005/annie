import asyncio
import json
import unittest
from robot_backend.app.config import Settings
from robot_backend.app.schemas import RemoteRequest, SummaryAnalysis
from robot_backend.app.services.agent import Agent
from robot_backend.app.sessions.manager import InMemorySessionManager

class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_retains_received_reply_and_erases_after_summary(self):
        entered = asyncio.Event()
        class Model:
            async def reply(self, messages):
                entered.set()
                await asyncio.Future()
            async def structured(self, messages, schema):
                self.context = json.loads(messages[-1]['content'])
                return SummaryAnalysis(status='inconclusive', summary='The resident answered, but the requested location was not established.')
        class Sink:
            async def enqueue(self, event): self.event = event
        settings=Settings(_env_file=None)
        sessions=InMemorySessionManager(settings)
        session=await sessions.create(RemoteRequest(request_id='test',type='message',request='Check location'))
        model,sink=Model(),Sink()
        agent=Agent(settings,sessions,model,sink,None)
        task=asyncio.create_task(agent.generate(session.session_id,[{'type':'text','text':'I am here'}]))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(session.user_turns,1)
        self.assertEqual(session.pending_resident_text,'I am here')
        await agent.end(session.session_id)
        self.assertEqual(model.context['received_unassessed_reply'],'I am here')
        self.assertIn('location',sink.event.summary)
        self.assertEqual(sink.event.status,'inconclusive')
        self.assertEqual(session.pending_resident_text,'')

    async def test_analysis_outage_keeps_prior_evidence_but_unassessed_reply_blocks_success(self):
        from robot_backend.app.services.qwen import QwenError
        class Model:
            async def reply(self, messages): return 'Thank you'
            async def structured(self, messages, schema):
                if schema is SummaryAnalysis:
                    return SummaryAnalysis(status='completed',summary='Task completed.')
                raise QwenError('Synthetic outage')
        class Speech:
            async def synthesize(self, text, sample_rate): return 'synthetic'
        class Sink:
            async def enqueue(self, event): self.event=event
        settings=Settings(_env_file=None)
        sessions=InMemorySessionManager(settings)
        session=await sessions.create(RemoteRequest(request_id='test',type='message',request='Check location'))
        session.goal_supported=True
        session.user_turns=1
        sink=Sink()
        agent=Agent(settings,sessions,Model(),sink,Speech())
        await agent.generate(session.session_id,[{'type':'text','text':'Actually that changed'}])
        self.assertTrue(session.goal_supported)
        self.assertEqual(session.pending_resident_text,'Actually that changed')
        await agent.end(session.session_id)
        self.assertEqual(sink.event.status,'inconclusive')
