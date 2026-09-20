"""Mute HTTP and runtime regressions with fake transport and no live audio."""
import asyncio
import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robot.dog.runtime import patrol
from robot.dog.voice.cloud import CloudVoice
from test_dog_control_lifecycle import start_runtime
from test_go2_patrol_greet_runtime import FakeDog


@pytest.fixture
def muted_voice(monkeypatch):
    speaker = Mock(return_value=True)
    voice = CloudVoice(eleven_key="", deepgram_key="", muted=True, local_speak=speaker)
    monkeypatch.setattr(patrol, "VOICE", voice)
    return voice, speaker


@pytest.fixture
def voice_http(monkeypatch, muted_voice):
    from robot.dog.voice import devices
    device = Mock()
    device.status.return_value = {"input": "mock mic", "output": "mock speaker"}
    monkeypatch.setattr(devices, "shared", lambda: device)
    server_class = patrol.ThreadingHTTPServer
    monkeypatch.setattr(patrol, "ThreadingHTTPServer", lambda address, handler: server_class(("127.0.0.1", 0), handler))
    view = patrol.LiveView(port=1, token="test-secret")
    view.listener = SimpleNamespace(open_until=99.0)
    view.start()
    url = f"http://127.0.0.1:{view.server.server_address[1]}/voice"

    def request(payload=None, token="test-secret"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Body-Token"] = token
        req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    yield view, request
    view.server.shutdown()
    view.server.server_close()


def test_authenticated_mute_round_trip_closes_conversation(voice_http, muted_voice):
    view, request = voice_http
    voice, _ = muted_voice
    status, body = request({"muted": False})
    assert status == 200 and body["muted"] is False
    assert voice.status()["muted"] is False
    assert view.listener.open_until == 99.0
    status, body = request({"muted": True})
    assert status == 200 and body["muted"] is True
    assert view.listener.open_until == 0.0
    assert request()[1]["muted"] is True


@pytest.mark.parametrize("token", [None, "wrong-secret"])
def test_mute_requires_token(voice_http, muted_voice, token):
    view, request = voice_http
    status, _ = request({"muted": False}, token=token)
    assert status == 401
    assert muted_voice[0].status()["muted"] is True
    assert view.listener.open_until == 99.0


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
def test_non_boolean_mute_rejected_without_state_change(voice_http, muted_voice, invalid):
    view, request = voice_http
    status, body = request({"muted": invalid})
    assert status == 400 and body["error"] == "muted must be a boolean"
    assert muted_voice[0].status()["muted"] is True
    assert view.listener.open_until == 99.0


def test_muted_speech_does_not_open_conversation(monkeypatch, muted_voice):
    listener = Mock()
    monkeypatch.setattr(patrol, "LISTENER", listener)
    assert patrol.speak_blocking("Do you hear me?") is False
    muted_voice[1].assert_not_called()
    listener.mute.assert_not_called()
    listener.open_conversation.assert_not_called()


def test_failed_playback_does_not_open_conversation(monkeypatch):
    voice, listener = Mock(), Mock()
    voice.status.return_value = {"muted": False}
    voice.speak.return_value = False
    monkeypatch.setattr(patrol, "VOICE", voice)
    monkeypatch.setattr(patrol, "LISTENER", listener)
    assert patrol.speak_blocking("Do you hear me?") is False
    listener.open_conversation.assert_not_called()


def test_muted_say_receipt_fails_without_spoken_dialogue(monkeypatch, muted_voice):
    dog, view = FakeDog(step_m=0.001), patrol.LiveView(port=0)
    monkeypatch.setattr(patrol, "LISTENER", Mock())

    async def run():
        task = await start_runtime(monkeypatch, dog, view, None, duration=0.8)
        code, _ = view.missions.submit({"command_id": "muted-say", "name": "say", "args": {"text": "Please take your medicine."}})
        assert code == 202
        while view.missions.get("muted-say")["state"] not in ("failed", "completed"):
            assert not task.done()
            await asyncio.sleep(0.002)
        receipt = view.missions.get("muted-say")
        assert receipt["state"] == "failed"
        assert receipt["result"] is not None, json.dumps(receipt)
        assert receipt["result"]["played"] is False
        assert "muted" in receipt["error"]
        assert not any(line["text"] == "Please take your medicine." for line in view.dialogue)
        report = await asyncio.wait_for(task, 5)
        assert report["reason"] == "duration_complete"

    asyncio.run(run())
    muted_voice[1].assert_not_called()


def test_muted_background_speech_does_not_spawn_playback(monkeypatch, muted_voice):
    thread = Mock()
    monkeypatch.setattr(patrol.threading, "Thread", thread)
    assert patrol._speak_host("Hello") is False
    thread.assert_not_called()
    muted_voice[1].assert_not_called()
