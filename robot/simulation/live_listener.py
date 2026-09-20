"""Live microphone reply for an active check-in: VAD-gated, bounded, cited.

This is the microphone counterpart of resident_reply.replay(). It records from
the host microphone only while a check-in is awaiting a reply, ends the window
after a silence hangover (kept long for slow, quiet speakers), caps it at the
smaller of --max-ms and the incident deadline, and transcribes only when at
least min_speech_ms of voice activity was heard. Silence is never sent to the
recognizer, because Whisper invents text on silence, and an invented "I'm fine"
would suppress escalation. A window with no speech is still reported, with an
empty transcript, so the incident policy sees a real capture with no answer.

Source is labelled `microphone`; it is never presented as a replay. Audio
bytes stay in process; the result carries the transcript and timing only.
The outcome is a perception estimate (answered / no speech), not a diagnosis.
"""
from __future__ import annotations

import argparse
import io
import math
import threading
import time
import wave
from uuid import UUID, uuid4

import httpx
import numpy as np

RATE = 16000
CHUNK_SAMPLES = 512  # Silero VAD window at 16 kHz (32 ms)
DEFAULT_MAX_MS = 8000
DEFAULT_MIN_SPEECH_MS = 300
DEFAULT_HANGOVER_MS = 700
START_THRESHOLD = 0.5
MODEL_LABEL = 'faster-whisper-tiny.en'
_LOCK = threading.Lock()


class ListenError(Exception):
    pass


def energy_vad(chunk: np.ndarray, threshold: float = 300.0) -> float:
    """Fallback voice-activity proxy: RMS energy of int16 samples, as 0/1."""
    rms = math.sqrt(float(np.mean(chunk.astype(np.float32) ** 2))) if chunk.size else 0.0
    return 1.0 if rms >= threshold else 0.0


def silero_vad():
    """Per-chunk speech probability from the Silero VAD bundled with faster-whisper.

    Falls back to energy_vad when onnxruntime or the model is unavailable.
    """
    try:
        from faster_whisper.vad import get_vad_model
        model = get_vad_model()
    except Exception:
        return energy_vad

    def vad(chunk: np.ndarray) -> float:
        audio = chunk.astype(np.float32) / 32768.0
        if audio.shape[0] != CHUNK_SAMPLES:
            return energy_vad(chunk)
        try:
            out = np.asarray(model(audio, num_samples=CHUNK_SAMPLES)).reshape(-1)
            return float(out[0])
        except Exception:
            return energy_vad(chunk)
    return vad


def sounddevice_recorder(device=None, rate: int = RATE, chunk: int = CHUNK_SAMPLES):
    """Yield int16 mono chunks from the host microphone (real path only)."""
    import queue

    import sounddevice as sd
    frames: queue.Queue = queue.Queue()

    def callback(indata, count, time_info, status):
        frames.put(indata[:, 0].copy())

    with sd.InputStream(samplerate=rate, channels=1, dtype='int16', blocksize=chunk,
                        device=device, callback=callback):
        while True:
            yield frames.get()


def pcm16_to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


def capture_utterance(recorder, vad, *, max_ms: int = DEFAULT_MAX_MS, min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
                      hangover_ms: int = DEFAULT_HANGOVER_MS, start_threshold: float = START_THRESHOLD,
                      monotonic=time.monotonic, rate: int = RATE) -> dict:
    """Consume chunks until speech ends, the window fills, or nothing is heard.

    Returns pcm bytes of the utterance (with the silence hangover trimmed to
    the chunks that surrounded speech), timing in ms relative to the window
    start, and outcome 'speech' or 'no_speech'. Never raises for a quiet room.
    """
    started = None  # the window starts when the first chunk arrives, not when the stream opens
    chunk_ms = 1000.0 * CHUNK_SAMPLES / rate
    kept: list[bytes] = []
    speech_ms = 0.0
    silence_ms = 0.0
    started_ms = None
    ended_ms = None
    elapsed_ms = 0.0
    for chunk in recorder:
        chunk = np.asarray(chunk, dtype=np.int16)
        if started is None:
            started = monotonic() - chunk_ms / 1000.0
        elapsed_ms = (monotonic() - started) * 1000.0
        voiced = float(vad(chunk)) >= start_threshold
        if voiced:
            if started_ms is None:
                started_ms = elapsed_ms - chunk_ms
            speech_ms += chunk_ms
            silence_ms = 0.0
            ended_ms = elapsed_ms
            kept.append(chunk.tobytes())
        elif started_ms is not None:
            silence_ms += chunk_ms
            kept.append(chunk.tobytes())
            if silence_ms >= hangover_ms:
                break
        if elapsed_ms >= max_ms:
            break
    outcome = 'speech' if speech_ms >= min_speech_ms else 'no_speech'
    return {'outcome': outcome, 'pcm': b''.join(kept) if outcome == 'speech' else b'',
            'speech_ms': int(speech_ms), 'elapsed_ms': int(elapsed_ms),
            'started_ms': None if started_ms is None else int(max(0, started_ms)),
            'ended_ms': None if ended_ms is None else int(ended_ms)}


def listen(*, app, recorder, vad, transcriber, event_id=None, clock=time.time, monotonic=time.monotonic,
           max_ms: int = DEFAULT_MAX_MS, min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
           hangover_ms: int = DEFAULT_HANGOVER_MS) -> dict:
    """Answer the active check-in from the microphone; one listener at a time."""
    if not _LOCK.acquire(blocking=False):
        raise ListenError('A microphone reply is already being captured')
    try:
        response = app.get('/status')
        response.raise_for_status()
        pending = response.json().get('pending_checkin')
        if not pending or pending.get('phase') != 'awaiting_reply':
            raise ListenError('No check-in is awaiting a resident reply')
        active_id = str(UUID(pending['event_id']))
        if event_id is not None and str(UUID(event_id)) != active_id:
            raise ListenError('The selected incident is no longer active')
        started_at = int(clock() * 1000)
        window_ms = int(min(max_ms, pending['deadline_at'] - started_at))
        if window_ms <= 0:
            raise ListenError('The reply deadline has already passed')
        identity = str(uuid4())
        capture = {'utterance_id': identity, 'event_id': active_id,
                   'source': 'microphone', 'capture_started_at': started_at}
        registered = app.post('/voice/capture', json=capture)
        registered.raise_for_status()
        heard = capture_utterance(recorder, vad, max_ms=window_ms, min_speech_ms=min_speech_ms,
                                  hangover_ms=hangover_ms, monotonic=monotonic)
        ended_at = int(clock() * 1000)
        if ended_at <= started_at:
            ended_at = started_at + max(1, heard['elapsed_ms'])
        text, segments, recognition = '', [], None
        if heard['outcome'] == 'speech':
            result = transcriber(pcm16_to_wav(heard['pcm']), utterance_id=identity)
            if getattr(result, 'utterance_id', identity) != identity or result.model != 'tiny.en':
                raise ListenError('Recognition identity or model does not match the capture')
            text = result.text
            segments = [{'start': s.start, 'end': s.end, 'text': s.text,
                         'avg_logprob': s.avg_logprob, 'no_speech_prob': s.no_speech_prob}
                        for s in result.segments]
            recognition = {'text': text, 'model': result.model, 'latency_ms': result.latency_ms,
                           'segments': len(segments)}
        payload = {**capture, 'capture_ended_at': ended_at, 'text': text, 'segments': segments,
                   'model': MODEL_LABEL}
        decision = app.post('/voice/result', json=payload)
        decision.raise_for_status()
        return {'source': 'microphone', 'utterance_id': identity, 'event_id': active_id,
                'outcome': heard['outcome'], 'speech_ms': heard['speech_ms'], 'window_ms': window_ms,
                'capture_started_at': started_at, 'capture_ended_at': ended_at,
                'recognition': recognition, 'decision': decision.json(),
                'completed_at': int(clock() * 1000)}
    except (ValueError, KeyError, httpx.HTTPError) as exc:
        raise ListenError(f'Microphone reply failed ({type(exc).__name__})') from None
    finally:
        _LOCK.release()


def _default_transcriber():
    from robot.simulation.local_stt import LocalSTTAdapter
    adapter = LocalSTTAdapter()
    adapter.warm()
    return adapter.transcribe


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Live microphone reply listener (VAD-gated).')
    parser.add_argument('--app', default='http://127.0.0.1:8000', help='app backend URL')
    parser.add_argument('--standalone', action='store_true',
                        help='record one utterance and print the transcript; no check-in required')
    parser.add_argument('--max-ms', type=int, default=DEFAULT_MAX_MS)
    parser.add_argument('--device', default=None, help='sounddevice input device name or index')
    args = parser.parse_args(argv)
    if not 500 <= args.max_ms <= 20000:
        parser.error('--max-ms must be 500-20000')
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    transcriber = _default_transcriber()
    vad = silero_vad()
    recorder = sounddevice_recorder(device=device)
    if args.standalone:
        print('listening...', flush=True)
        heard = capture_utterance(recorder, vad, max_ms=args.max_ms)
        if heard['outcome'] != 'speech':
            print({'outcome': heard['outcome'], 'elapsed_ms': heard['elapsed_ms'], 'speech_ms': heard['speech_ms']})
            return 1
        result = transcriber(pcm16_to_wav(heard['pcm']), utterance_id=str(uuid4()))
        print({'outcome': 'speech', 'text': result.text, 'speech_ms': heard['speech_ms'],
               'latency_ms': round(result.latency_ms, 1), 'model': result.model})
        return 0
    with httpx.Client(base_url=args.app, timeout=5, trust_env=False) as app:
        try:
            result = listen(app=app, recorder=recorder, vad=vad, transcriber=transcriber, max_ms=args.max_ms)
        except ListenError as exc:
            print(str(exc))
            return 1
    print(result)
    return 0 if result['decision'].get('eligible') else 1


if __name__ == '__main__':
    raise SystemExit(main())
