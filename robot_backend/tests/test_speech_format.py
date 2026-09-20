import base64
import io
import unittest
import wave

import httpx

from robot_backend.app.config import Settings
from robot_backend.app.services.deepgram import DeepgramClient, SpeechError
from robot_backend.app.services.live_audio import pcm_from_wav


class SpeechFormatTests(unittest.IsolatedAsyncioTestCase):
    async def synthesize(self, pcm, rate):
        async def handler(request):
            self.assertEqual(request.url.params['container'], 'none')
            self.assertEqual(request.url.params['encoding'], 'linear16')
            self.assertEqual(request.url.params['sample_rate'], str(rate))
            return httpx.Response(200, content=pcm)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepgramClient(Settings(_env_file=None, deepgram_api_key='fake'), client).synthesize('Synthetic test.', sample_rate=rate)

    async def test_phone_pcm_roundtrip_preserves_samples(self):
        pcm = b'\x01\x00\xff\xff' * 320
        encoded = await self.synthesize(pcm, 16000)
        self.assertEqual(pcm_from_wav(encoded), pcm)

    async def test_rest_still_returns_finalized_24khz_wav(self):
        pcm = b'\x00\x00' * 240
        encoded = await self.synthesize(pcm, 24000)
        with wave.open(io.BytesIO(base64.b64decode(encoded))) as audio:
            self.assertEqual(audio.getframerate(), 24000)
            self.assertEqual(audio.getnframes(), 240)
            self.assertEqual(audio.readframes(240), pcm)

    async def test_rejects_empty_odd_and_unexpected_container(self):
        for data in (b'', b'\x01', b'RIFF1234WAVE'):
            with self.subTest(data=data), self.assertRaises(SpeechError):
                await self.synthesize(data, 16000)
