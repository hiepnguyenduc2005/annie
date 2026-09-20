import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from robot_backend.app.config import Settings
from robot_backend.app.main import create_app
from robot_backend.app.services.deepgram import DeepgramClient, SpeechError
from robot_backend.app.services.qwen import QwenClient
from robot_backend.app.sessions.manager import InMemorySessionManager


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 240)
    return buffer.getvalue()


class Sink:
    def __init__(self):
        self.events = []

    async def enqueue(self, event):
        self.events.append(event)

    async def deliver_pending(self):
        pass


class SpeechPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(
            _env_file=None,
            deepgram_api_key="test-key",
            qwen_base_url="http://qwen.test",
            deepgram_base_url="https://deepgram.test",
            outbox_path=Path(self.directory.name) / "outbox.sqlite3",
            maintenance_interval_seconds=3600,
        )
        self.requests = []
        self.tts_failure = False
        self.stt_text = "I had breakfast."
        self.model_failure = False
        self.done = False
        self.bad_wav = False
        self.transport = httpx.MockTransport(self.respond)
        # MockTransport owns no sockets; TestClient invokes the adapters on its loop.
        self.http = httpx.AsyncClient(transport=self.transport)
        self.store = InMemorySessionManager(self.settings)
        self.sink = Sink()
        app = create_app(
            self.settings,
            model=QwenClient(self.settings, self.http),
            speech=DeepgramClient(self.settings, self.http),
            sink=self.sink,
            sessions=self.store,
        )
        self.client = self.enterContext(TestClient(app))

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/listen":
            self.assertEqual(request.headers["authorization"], "Token test-key")
            self.assertEqual(request.url.params["model"], "nova-3")
            return httpx.Response(
                200,
                json={
                    "results": {
                        "channels": [{"alternatives": [{"transcript": self.stt_text}]}]
                    }
                },
            )
        if request.url.path == "/v1/speak":
            self.assertEqual(json.loads(request.content), {"text": "I'm glad you ate."})
            self.assertEqual(request.url.params["container"], "wav")
            self.assertEqual(request.url.params["encoding"], "linear16")
            if self.tts_failure:
                return httpx.Response(429, text="PRIVATE PROVIDER BODY")
            return httpx.Response(200, content=b"bad audio" if self.bad_wav else wav())
        self.assertEqual(request.url.path, "/v1/chat/completions")
        body = json.loads(request.content)
        self.assertEqual(body["modalities"], ["text"])
        self.assertNotIn("audio_url", request.content.decode())
        if self.model_failure:
            return httpx.Response(503, text="PRIVATE MODEL BODY")
        if body["max_tokens"] == 500:
            content = "I'm glad you ate."
        elif "Return ONLY JSON with status" in body["messages"][0]["content"]:
            content = json.dumps(
                {"status": "completed", "summary": "Grandma reported having breakfast."}
            )
        else:
            content = json.dumps(
                {
                    "conversation_done": self.done,
                    "task_status": "completed" if self.done else "active",
                    "rolling_memory": "Reported breakfast.",
                    "user_memory": "Reported breakfast.",
                    "assistant_memory": "Acknowledged breakfast.",
                    "goal_supported": True,
                }
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    def test_audio_transcribed_once_and_only_spoken_reply_synthesized(self):
        response = self.client.post(
            "/generate", files={"audio": ("input.wav", wav(), "audio/wav")}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"session_id", "audio", "done"})
        self.assertEqual(base64.b64decode(response.json()["audio"]), wav())
        paths = [r.url.path for r in self.requests]
        self.assertEqual(
            paths,
            ["/v1/listen", "/v1/chat/completions", "/v1/speak", "/v1/chat/completions"],
        )
        for request in self.requests:
            if request.url.host == "qwen.test":
                self.assertIn("I had breakfast.", request.content.decode())
        session = self.store._sessions[response.json()["session_id"]]
        self.assertNotIn("base64", json.dumps(session.history))

    def test_text_only_skips_transcription(self):
        response = self.client.post("/generate", data={"text": "Hello"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/v1/listen", [r.url.path for r in self.requests])

    def test_combined_text_and_audio(self):
        response = self.client.post(
            "/generate",
            data={"text": "Additional context"},
            files={"audio": ("a.wav", wav(), "audio/wav")},
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(self.requests[1].content)
        self.assertEqual(
            body["messages"][-1]["content"],
            [
                {"type": "text", "text": "Additional context"},
                {"type": "text", "text": "I had breakfast."},
            ],
        )

    def test_image_rejected_before_provider_calls_and_absent_from_schema(self):
        for data in ({}, {"text": "Hello"}):
            result = self.client.post(
                "/generate",
                data=data,
                files={"image": ("a.jpg", b"image", "image/jpeg")},
            )
            self.assertEqual(result.status_code, 422)
        self.assertFalse(self.requests)
        schema = self.client.get("/openapi.json").json()
        ref = schema["paths"]["/generate"]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]["schema"]["$ref"].split("/")[-1]
        self.assertNotIn("image", schema["components"]["schemas"][ref]["properties"])

    def test_tts_failure_does_not_advance_existing_session(self):
        sid = self.client.post("/generate", data={"text": "Hello"}).json()["session_id"]
        previous = list(self.store._sessions[sid].history)
        self.tts_failure = True
        result = self.client.post(
            "/generate", data={"session_id": sid, "text": "Goodbye"}
        )
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("PRIVATE", result.text)
        self.assertEqual(self.store._sessions[sid].history, previous)
        self.assertFalse(self.sink.events)

    def test_silence_does_not_create_a_turn(self):
        self.stt_text = " "
        result = self.client.post(
            "/generate", files={"audio": ("a.wav", wav(), "audio/wav")}
        )
        self.assertEqual(result.status_code, 422)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.store._sessions)

    def test_missing_key_clean_error(self):
        from pydantic import SecretStr

        self.settings.deepgram_api_key = SecretStr("")
        result = self.client.post(
            "/generate", files={"audio": ("a.wav", wav(), "audio/wav")}
        )
        self.assertEqual(result.status_code, 503)
        self.assertFalse(self.requests)
        self.assertFalse(self.store._sessions)

    def test_bad_wav_and_qwen_failure(self):
        self.bad_wav = True
        result = self.client.post("/generate", data={"text": "Hello"})
        self.assertEqual(result.status_code, 502)
        self.assertFalse(self.store._sessions)
        self.model_failure = True
        result = self.client.post("/generate", data={"text": "Hello"})
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("PRIVATE", result.text)

    def test_goal_finalization_and_summary_stay_separate_from_speech(self):
        sid = self.client.post(
            "/requests",
            json={
                "request_id": "r1",
                "type": "check_in",
                "request": "Check breakfast.",
            },
        ).json()["session_id"]
        self.done = True
        result = self.client.post(
            "/generate", data={"session_id": sid, "text": "I ate. Bye."}
        )
        self.assertTrue(result.json()["done"])
        self.assertEqual(len(self.sink.events), 1)
        self.assertFalse(self.store._sessions[sid].history)
        self.assertEqual(self.client.post(f"/sessions/{sid}/end").status_code, 200)
        self.assertEqual(len(self.sink.events), 1)


class AdapterFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_transcription_and_network_failure(self):
        settings = Settings(_env_file=None, deepgram_api_key="test")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ) as client:
            with self.assertRaises(SpeechError):
                await DeepgramClient(settings, client).transcribe(wav(), "audio/wav")

        def timeout(request):
            raise httpx.ReadTimeout("PRIVATE", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(SpeechError) as error:
                await DeepgramClient(settings, client).synthesize("Hello")
            self.assertNotIn("PRIVATE", str(error.exception))


if __name__ == "__main__":
    unittest.main()
