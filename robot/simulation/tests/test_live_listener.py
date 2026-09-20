"""Fakes-only tests for the live microphone reply listener: no mic, no model, no network."""
import io
import json
import wave
from uuid import uuid4

import httpx
import numpy as np
import pytest

from robot.simulation.live_listener import (ListenError, capture_utterance, energy_vad, listen,
                                            pcm16_to_wav)

RATE = 16000
CHUNK = 512  # samples per VAD window (32 ms at 16 kHz)


def chunks(probs):
    """A recorder yielding one 512-sample chunk per scripted VAD probability."""
    for i, _ in enumerate(probs):
        yield (np.full(CHUNK, 1000 if probs[i] >= 0.5 else 0, dtype=np.int16))


def scripted_vad(probs):
    it = iter(probs)
    return lambda chunk: next(it)


class Clock:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def run_capture(probs, **kw):
    clock = Clock()

    def recorder():
        for chunk in chunks(probs):
            clock.advance(CHUNK / RATE)
            yield chunk
        while True:  # a real mic never ends; the capture must stop itself
            clock.advance(CHUNK / RATE)
            yield np.zeros(CHUNK, dtype=np.int16)

    return capture_utterance(recorder(), scripted_vad(probs + [0.0] * 10_000), monotonic=clock.monotonic,
                             **{'max_ms': 8000, 'min_speech_ms': 300, 'hangover_ms': 700, **kw})


def test_speech_then_silence_ends_after_hangover_and_keeps_only_that_utterance():
    probs = [0.0] * 5 + [0.9] * 20 + [0.0] * 40  # 0.16 s silence, 0.64 s speech, then silence
    result = run_capture(probs)
    assert result['outcome'] == 'speech'
    assert 600 <= result['speech_ms'] <= 700
    assert result['ended_ms'] - result['started_ms'] < 2000
    # Stopped ~700 ms after speech ended, well before the 8 s cap.
    assert result['elapsed_ms'] < 2500
    assert len(result['pcm']) % 2 == 0 and len(result['pcm']) >= 20 * CHUNK * 2


def test_too_short_speech_is_no_speech_and_pcm_is_not_transcribed():
    probs = [0.0] * 5 + [0.9] * 3 + [0.0] * 60  # ~96 ms blip
    result = run_capture(probs)
    assert result['outcome'] == 'no_speech' and result['speech_ms'] < 300
    assert result['pcm'] == b''


def test_silence_only_stops_at_max_window():
    result = run_capture([0.0] * 400, max_ms=1000)
    assert result['outcome'] == 'no_speech' and 1000 <= result['elapsed_ms'] <= 1100


def test_long_speech_is_cut_at_max_window():
    result = run_capture([0.9] * 400, max_ms=2000)
    assert result['outcome'] == 'speech' and 2000 <= result['elapsed_ms'] <= 2100


def test_energy_vad_distinguishes_silence_from_tone():
    assert energy_vad(np.zeros(CHUNK, dtype=np.int16)) < 0.5
    assert energy_vad(np.full(CHUNK, 2000, dtype=np.int16)) >= 0.5


def test_pcm16_to_wav_roundtrip():
    wav_bytes = pcm16_to_wav(b'\x01\x00' * 1600, RATE)
    with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
        assert wav.getframerate() == RATE and wav.getnchannels() == 1 and wav.getnframes() == 1600


# ---- listen(): the check-in reply path -------------------------------------

class FakeTranscription:
    def __init__(self, text, utterance_id):
        self.text, self.utterance_id, self.model, self.latency_ms = text, utterance_id, 'tiny.en', 90.0
        self.segments = [type('S', (), {'start': 0.0, 'end': 1.0, 'text': text, 'avg_logprob': -0.5,
                                        'no_speech_prob': 0.1})()] if text else []


def rig(*, pending=True, remaining=8000, probs=None):
    seen = []
    clock = Clock()
    event_id = str(uuid4())

    def app_handler(request):
        value = json.loads(request.content) if request.content else None
        seen.append((request.url.path, value))
        if request.url.path == '/status':
            checkin = {'event_id': event_id, 'phase': 'awaiting_reply',
                       'deadline_at': int(clock.monotonic() * 1000) + remaining}
            return httpx.Response(200, json={'pending_checkin': checkin if pending else None})
        if request.url.path == '/voice/result':
            return httpx.Response(200, json={'eligible': True, 'applied': True, 'intent': 'concern'})
        return httpx.Response(200, json={'status': 'registered'})

    probs = probs if probs is not None else [0.0] * 5 + [0.9] * 20 + [0.0] * 40

    def recorder():
        for chunk in chunks(probs):
            clock.advance(CHUNK / RATE)
            yield chunk
        while True:
            clock.advance(CHUNK / RATE)
            yield np.zeros(CHUNK, dtype=np.int16)

    transcribed = []

    def transcriber(wav_bytes, *, utterance_id):
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
            assert wav.getframerate() == RATE
        transcribed.append(utterance_id)
        return FakeTranscription('Not okay.', utterance_id)

    options = {'app': httpx.Client(base_url='http://app', transport=httpx.MockTransport(app_handler)),
               'recorder': recorder(), 'vad': scripted_vad(probs + [0.0] * 10_000), 'transcriber': transcriber,
               'clock': clock.monotonic, 'monotonic': clock.monotonic}
    return options, seen, event_id, transcribed


def test_listen_registers_microphone_capture_and_submits_recognized_text():
    options, seen, event_id, transcribed = rig()
    result = listen(**options)
    assert [path for path, _ in seen] == ['/status', '/voice/capture', '/voice/result']
    capture, sent = seen[1][1], seen[2][1]
    assert capture['source'] == 'microphone' and capture['event_id'] == event_id
    assert sent['source'] == 'microphone' and sent['model'] == 'faster-whisper-tiny.en'
    assert sent['text'] == 'Not okay.' and sent['utterance_id'] == capture['utterance_id']
    assert sent['capture_ended_at'] > sent['capture_started_at']
    assert transcribed == [capture['utterance_id']]
    assert result['outcome'] == 'speech' and result['decision']['intent'] == 'concern'
    assert 'pcm' not in result and 'audio_b64' not in json.dumps(result)


def test_listen_with_no_speech_submits_empty_text_without_transcribing():
    options, seen, event_id, transcribed = rig(probs=[0.0] * 30)
    options['max_ms'] = 500
    result = listen(**options)
    assert [path for path, _ in seen] == ['/status', '/voice/capture', '/voice/result']
    assert seen[2][1]['text'] == '' and seen[2][1]['segments'] == []
    assert transcribed == [] and result['outcome'] == 'no_speech'


def test_listen_refuses_without_an_awaiting_checkin():
    options, seen, _, _ = rig(pending=False)
    with pytest.raises(ListenError):
        listen(**options)
    assert [path for path, _ in seen] == ['/status']


def test_listen_bounds_the_window_by_the_reply_deadline():
    options, seen, _, _ = rig(remaining=1000, probs=[0.0] * 400)
    result = listen(**options)
    assert result['outcome'] == 'no_speech' and result['window_ms'] <= 1000
