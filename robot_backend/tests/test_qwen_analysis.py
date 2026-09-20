import json
import unittest

import httpx

from robot_backend.app.config import Settings
from robot_backend.app.schemas import TurnAnalysis
from robot_backend.app.services.qwen import QwenClient, QwenError


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def analyze(self, output, status=200):
        async def handler(request):
            self.request = json.loads(request.content)
            return httpx.Response(status, json={'choices': [{'message': {'content': output}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await QwenClient(Settings(_env_file=None), client).structured(
                [{'role': 'assistant', 'content': 'Synthetic reply'}], TurnAnalysis)

    async def test_sends_explicit_schema_request_and_accepts_fenced_json(self):
        body = dict(conversation_done=False, task_status='active', rolling_memory='',
                    user_memory='', assistant_memory='', goal_supported=False)
        result = await self.analyze('```json\n' + json.dumps(body) + '\n```')
        self.assertFalse(result.goal_supported)
        self.assertEqual(self.request['messages'][-1]['role'], 'user')
        self.assertIn('required', self.request['messages'][-1]['content'])

    async def test_missing_fields_not_filled_with_invented_success(self):
        with self.assertRaisesRegex(QwenError, 'responded.*missing'):
            await self.analyze('{}')

    async def test_prose_rejected_without_leaking_input(self):
        with self.assertRaises(QwenError) as error:
            await self.analyze('private synthetic content')
        self.assertIn('json_invalid', str(error.exception))
        self.assertNotIn('private synthetic content', str(error.exception))

    async def test_http_failure_distinguished_from_validation(self):
        with self.assertRaisesRegex(QwenError, 'unavailable or rejected'):
            await self.analyze('private synthetic content', status=503)
