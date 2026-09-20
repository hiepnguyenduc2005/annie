"""Fakes-only tests for the MLX vision server: no model, no MLX import."""
import base64
import json
import threading
import time

from fastapi.testclient import TestClient

from robot.robot_backend.mlx_vision_server import create_app, extract_json

JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()


def chat_payload(system="Return only JSON.", text="Report the person.", jpeg=JPEG_B64, **extra):
    return {"model": "any", "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + jpeg}}]}],
        "max_tokens": 128, "temperature": 0, "response_format": {"type": "json_object"}, **extra}


class FakeEngine:
    def __init__(self, reply='{"person": false, "posture": "unknown", "location": "unknown", "confidence": 0.9, "caption": "empty room"}',
                 delay=0.0):
        self.reply, self.delay, self.calls = reply, delay, []
        self.model_id = "fake/model"

    def generate(self, prompt, image_data_url, max_tokens):
        self.calls.append({"prompt": prompt, "image": image_data_url, "max_tokens": max_tokens})
        time.sleep(self.delay)
        return {"text": self.reply, "prompt_tokens": 120, "generation_tokens": 30, "finished": True}


def test_chat_completion_maps_openai_shape_and_passes_prompt_and_image():
    engine = FakeEngine()
    with TestClient(create_app(engine)) as api:
        response = api.post("/v1/chat/completions", json=chat_payload())
    assert response.status_code == 200, response.text
    data = response.json()
    choice = data["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert json.loads(choice["message"]["content"])["caption"] == "empty room"
    assert data["usage"] == {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    assert data["model"] == "fake/model"
    call = engine.calls[0]
    assert "Return only JSON." in call["prompt"] and "Report the person." in call["prompt"]
    assert call["image"].startswith("data:image/jpeg;base64,") and call["max_tokens"] == 128


def test_extra_text_around_json_is_trimmed_to_the_object():
    engine = FakeEngine(reply='Sure! {"person": true, "posture": "lying", "location": "floor", "confidence": 0.8, "caption": "x"} done')
    with TestClient(create_app(engine)) as api:
        content = api.post("/v1/chat/completions", json=chat_payload()).json()["choices"][0]["message"]["content"]
    assert json.loads(content)["posture"] == "lying"


def test_reply_without_json_is_returned_raw_with_length_finish_reason():
    engine = FakeEngine(reply="I cannot tell")
    engine.generate = lambda p, i, m: {"text": "I cannot tell", "prompt_tokens": 1, "generation_tokens": 128, "finished": False}
    with TestClient(create_app(engine)) as api:
        data = api.post("/v1/chat/completions", json=chat_payload()).json()
    assert data["choices"][0]["finish_reason"] == "length"
    assert data["choices"][0]["message"]["content"] == "I cannot tell"


def test_rejects_missing_image_and_bad_data_url():
    with TestClient(create_app(FakeEngine())) as api:
        no_image = {"model": "any", "messages": [{"role": "user", "content": "hi"}]}
        assert api.post("/v1/chat/completions", json=no_image).status_code == 422
        bad = chat_payload()
        bad["messages"][1]["content"][1]["image_url"]["url"] = "http://evil.example/x.jpg"
        assert api.post("/v1/chat/completions", json=bad).status_code == 422


def test_requests_are_serialized_one_at_a_time():
    engine = FakeEngine(delay=0.15)
    order = []
    with TestClient(create_app(engine)) as api:
        def worker(name):
            api.post("/v1/chat/completions", json=chat_payload())
            order.append(name)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started
    assert len(order) == 3 and elapsed >= 0.4  # three serialized 150 ms generations


def test_health_reports_model_and_ready():
    with TestClient(create_app(FakeEngine())) as api:
        data = api.get("/health").json()
    assert data == {"status": "ok", "model": "fake/model", "runtime": "mlx-vlm", "ready": True}


def test_extract_json_helper():
    assert extract_json('{"a": 1}') == ('{"a": 1}', True)
    assert extract_json('text {"a": {"b": 2}} tail') == ('{"a": {"b": 2}}', True)
    assert extract_json('no braces') == ('no braces', False)
    assert extract_json('{"unterminated": ') == ('{"unterminated": ', False)
