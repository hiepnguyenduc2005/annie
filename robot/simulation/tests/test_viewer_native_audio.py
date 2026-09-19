"""Targeted integration tests for --native-audio viewer wiring.

Fakes the NativeAudioPlayer and speech synthesis; never spawns afplay or say.
"""
from __future__ import annotations

import json as jsonlib
import threading
from types import SimpleNamespace

import robot.simulation.viewer as viewer


class FakePlayer:
    """Captures enqueues and emits callback states on demand."""

    def __init__(self):
        self.enqueued = []
        self.callbacks = {}
        self.lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def enqueue(self, clip, *, on_state_callback=None):
        with self.lock:
            self.enqueued.append(clip)
            self.callbacks[clip["file_path"]] = on_state_callback

    def emit(self, file_path, payload):
        self.callbacks[file_path](payload)


def make_handler(shared, port=8766):
    handler_class = viewer.handler_for(shared, port)
    handler = handler_class.__new__(handler_class)
    handler.reply = lambda status, body, ct="application/json": None
    # Mimic http.client.HTTPMessage.get_content_type().
    handler.headers = SimpleNamespace(
        get=lambda name, default=None: {
            "Content-Length": "0",
            "Transfer-Encoding": None,
        }.get(name, default),
        get_content_type=lambda: "application/json",
    )
    return handler


def call_speech_request(handler, path, value):
    """Drive speech_request without a socket; capture the reply."""
    captured = {}
    handler.reply = lambda status, body, ct="application/json": captured.update(
        status=status, body=body
    )
    body = jsonlib.dumps(value).encode()
    handler.headers.get = lambda name, default=None: {
        "Content-Length": str(len(body)),
        "Transfer-Encoding": None,
    }.get(name, default)

    class RFile:
        def read(self, n):
            return body

    handler.rfile = RFile()
    handler.connection = SimpleNamespace(settimeout=lambda t: None)
    handler.speech_request(path)
    return captured


CID = "00000000-0000-4000-8000-000000000001"


def make_native_clip(shared, player):
    clip = {
        "command_id": CID,
        "status": "synthesized",
        "file_path": "/tmp/fake/" + CID + ".wav",
        "duration_s": 0.2,
        "played": False,
        "playback": "native",
        "output": "macos_default_output",
    }
    with shared.lock:
        shared.speech[CID] = dict(clip)
    player.enqueue(
        {"file_path": clip["file_path"], "duration_s": clip["duration_s"]},
        on_state_callback=viewer.playback_callback_for(shared, CID),
    )


def native_shared():
    shared = viewer.Shared()
    shared.native_player = FakePlayer()
    return shared


def test_say_enqueues_once_and_callback_records_states():
    shared = native_shared()
    make_native_clip(shared, shared.native_player)
    player = shared.native_player
    assert len(player.enqueued) == 1
    assert player.enqueued[0]["duration_s"] == 0.2

    player.emit(
        player.enqueued[0]["file_path"],
        {"state": "playing", "monotonic": 1.0, "wall": 1000.0},
    )
    with shared.lock:
        assert shared.speech[CID]["status"] == "playing"
        assert shared.speech[CID]["playback_started_wall"] == 1000.0

    player.emit(
        player.enqueued[0]["file_path"],
        {"state": "played", "monotonic": 2.0, "wall": 1001.5},
    )
    with shared.lock:
        entry = shared.speech[CID]
        # Actual afplay exit 0 is the only path to this value.
        assert entry["status"] == "played"
        assert entry["playback_detail"] == "playback_process_completed"
        assert entry["playback_ended_wall"] == 1001.5


def test_speech_receipts_native_mapping():
    shared = native_shared()
    make_native_clip(shared, shared.native_player)

    with shared.lock:
        clips = [
            {k: v for k, v in item.items() if k != "file_path"}
            for item in shared.speech.values()
        ]
    receipts = viewer.speech_receipts(clips, native=True)
    assert receipts[0]["status"] == "executing"
    assert receipts[0]["detail"] == "Audio synthesized; awaiting native playback"

    with shared.lock:
        shared.speech[CID]["status"] = "failed"
        shared.speech[CID]["playback_error"] = "afplay missing"
    with shared.lock:
        clips = [
            {k: v for k, v in item.items() if k != "file_path"}
            for item in shared.speech.values()
        ]
    receipts = viewer.speech_receipts(clips, native=True)
    assert receipts[0]["status"] == "failed"
    assert receipts[0]["detail"] == "afplay missing"

    with shared.lock:
        shared.speech[CID]["status"] = "played"
        shared.speech[CID]["playback_detail"] = "playback_process_completed"
    with shared.lock:
        clips = [
            {k: v for k, v in item.items() if k != "file_path"}
            for item in shared.speech.values()
        ]
    receipts = viewer.speech_receipts(clips, native=True)
    assert receipts[0]["status"] == "completed"
    assert receipts[0]["detail"] == "playback_process_completed"


def test_browser_receipt_rejected_for_native_clip():
    shared = native_shared()
    make_native_clip(shared, shared.native_player)
    handler = make_handler(shared)
    captured = call_speech_request(handler, "/speech/played", {"command_id": CID})
    assert captured["status"] == 409
    with shared.lock:
        assert shared.speech[CID]["status"] != "played"


def test_browser_receipt_accepted_for_browser_clip():
    shared = viewer.Shared()
    cid = "00000000-0000-4000-8000-000000000002"
    with shared.lock:
        shared.speech[cid] = {"command_id": cid, "status": "synthesized", "played": False}
    handler = make_handler(shared)
    captured = call_speech_request(handler, "/speech/played", {"command_id": cid})
    assert captured["status"] == 200
    with shared.lock:
        assert shared.speech[cid]["status"] == "played"


def test_actual_say_route_resolves_path_and_does_not_reenqueue(monkeypatch):
    from pathlib import Path
    from robot.simulation.speech import SpeechAdapter

    async def synthesize(self, text, command_id):
        return {'command_id': command_id, 'status': 'synthesized', 'played': False,
                'duration_s': .4, 'file_path': f'.data/simulation/speech/{command_id}.wav'}

    monkeypatch.setattr(SpeechAdapter, 'speak', synthesize)
    shared = native_shared()
    handler = make_handler(shared)
    first = call_speech_request(handler, '/say', {'text': 'Hello.', 'command_id': CID})
    repeated = call_speech_request(handler, '/say', {'text': 'Hello.', 'command_id': CID})
    assert first['status'] == repeated['status'] == 202
    assert len(shared.native_player.enqueued) == 1
    assert Path(shared.native_player.enqueued[0]['file_path']).is_absolute()
    assert first['body']['playback'] == 'native'
