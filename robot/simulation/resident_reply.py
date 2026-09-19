"""Replay a synthetic resident WAV through local Whisper and the incident API.

This is a clocked file input, not microphone capture or speaker recognition.
The fixture's expected words never enter the recognition request or policy.
No cloud inference, fabricated confidence, or incident reset happens here.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import threading
import time
import wave
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

FIXTURE_TEXT = {'okay': 'Okay.', 'help': 'Help me.', 'not-okay': 'Not okay.',
                'ambiguous': 'Maybe.', 'echo': 'Are you okay? Please say okay or help.'}
KINDS = (*FIXTURE_TEXT, 'silence')
FIXTURE_DIR = Path('.data/simulation/resident-replies')
_LOCK = threading.Lock()


class ReplyError(Exception):
    pass


def fixture_path(kind, directory=FIXTURE_DIR):
    if kind not in KINDS:
        raise ReplyError('Unknown synthetic reply fixture')
    return Path(directory) / f'{kind}.wav'


async def prepare_fixtures(directory=FIXTURE_DIR):
    """Offline macOS synthesis, explicitly separate from dog speech commands."""
    from robot.simulation.speech import SpeechAdapter
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    adapter = SpeechAdapter(output_dir=directory)
    for kind, text in FIXTURE_TEXT.items():
        target = fixture_path(kind, directory)
        if target.exists():
            continue
        identity = str(uuid5(NAMESPACE_URL, f'annie:resident-reply:v1:{kind}'))
        clip = await adapter.speak(text, identity)
        Path(clip['file_path']).replace(target)
    target = fixture_path('silence', directory)
    if not target.exists():
        with wave.open(str(target), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0' * 16000)


def replay(kind, *, app, viewer, event_id=None, directory=FIXTURE_DIR,
           clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
    """Consume a known WAV at its sample rate; submit only recognized evidence.

    app/viewer are local HTTP clients or injected test transports. The lock
    permits one input at a time. Input failures propagate; they never become
    reassurance or a substituted transcript.
    """
    if not _LOCK.acquire(blocking=False):
        raise ReplyError('A synthetic reply is already being captured')
    try:
        path = fixture_path(kind, directory)
        if not path.is_file():
            raise ReplyError('Reply fixtures are not prepared; run resident_reply --prepare')
        if path.stat().st_size > 4_000_000:
            raise ReplyError('Reply fixture exceeds the WAV size limit')
        raw = path.read_bytes()
        with wave.open(str(path), 'rb') as wav:
            rate, frames = wav.getframerate(), wav.getnframes()
            duration = frames / rate
            if wav.getsampwidth() != 2 or wav.getnchannels() not in (1, 2) or not 8000 <= rate <= 48000 or not 0 < duration <= 20:
                raise ReplyError('Invalid PCM reply fixture')
            response = app.get('/status')
            response.raise_for_status()
            pending = response.json().get('pending_checkin')
            if not pending or pending.get('phase') != 'awaiting_reply':
                raise ReplyError('No check-in is awaiting a resident reply')
            active_id = str(UUID(pending['event_id']))
            if event_id is not None and str(UUID(event_id)) != active_id:
                raise ReplyError('The selected incident is no longer active')
            started_at = int(clock() * 1000)
            if started_at + duration * 1000 > pending['deadline_at']:
                raise ReplyError('Insufficient response time for this recording')
            identity = str(uuid4())
            capture = {'utterance_id': identity, 'event_id': active_id,
                       'source': 'synthetic_replay', 'capture_started_at': started_at}
            registered = app.post('/voice/capture', json=capture)
            registered.raise_for_status()
            start_mono = monotonic()
            consumed = 0
            # The replay clock consumes real PCM frames. It neither plays the
            # file through a speaker nor claims that a physical mic heard it.
            while consumed < frames:
                count = min(max(1, rate // 50), frames - consumed)
                chunk = wav.readframes(count)
                if len(chunk) != count * wav.getnchannels() * wav.getsampwidth():
                    raise ReplyError('Truncated reply fixture')
                consumed += count
                sleep(max(0, start_mono + consumed / rate - monotonic()))
            ended_at = int(clock() * 1000)
        recognition = viewer.post('/transcribe-local', json={
            'audio_b64': base64.b64encode(raw).decode(), 'format': 'wav',
            'source': 'simulation_audio', 'utterance_id': identity})
        recognition.raise_for_status()
        result = recognition.json()
        if result.get('utterance_id') != identity or result.get('model') != 'tiny.en':
            raise ReplyError('Recognition identity or model does not match the capture')
        payload = {**capture, 'capture_ended_at': ended_at,
                   'text': result['text'], 'segments': result['segments'],
                   'model': 'faster-whisper-' + result['model']}
        decision = app.post('/voice/result', json=payload)
        decision.raise_for_status()
        return {'source': 'synthetic_replay', 'fixture': kind, 'utterance_id': identity,
                'event_id': active_id, 'capture_started_at': started_at,
                'capture_ended_at': ended_at, 'duration_s': duration,
                'recognition': {key: result[key] for key in ('text', 'segments', 'model', 'latency_ms')},
                'decision': decision.json(), 'completed_at': int(clock() * 1000)}
    except (ValueError, KeyError, wave.Error, OSError, httpx.HTTPError) as exc:
        raise ReplyError(f'Resident replay failed ({type(exc).__name__})') from None
    finally:
        _LOCK.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--reply', choices=KINDS)
    parser.add_argument('--wait', type=float, default=0, help='Wait at most 60 seconds for the next reply window')
    args = parser.parse_args()
    if not 0 <= args.wait <= 60:
        parser.error('--wait must be in [0, 60] seconds')
    if args.prepare:
        asyncio.run(prepare_fixtures())
    if not args.reply:
        return
    token = os.getenv('ANNIE_API_TOKEN')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with httpx.Client(base_url='http://127.0.0.1:8000', headers=headers, timeout=5, trust_env=False) as app, \
            httpx.Client(base_url='http://127.0.0.1:8766', timeout=15, trust_env=False) as viewer:
        deadline = time.monotonic() + args.wait
        while True:
            pending = app.get('/status').json().get('pending_checkin')
            if pending and pending.get('phase') == 'awaiting_reply':
                break
            if time.monotonic() >= deadline:
                raise SystemExit('No check-in is awaiting a reply')
            time.sleep(.1)
        print(json.dumps(replay(args.reply, app=app, viewer=viewer), indent=2))


if __name__ == '__main__':
    main()
