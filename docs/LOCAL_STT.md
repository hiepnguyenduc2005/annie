# Local STT (simulation speech recognition)

Local, offline Whisper speech-to-text for simulated robot audio. The model is
faster-whisper `tiny.en` on CPU. It never calls a cloud provider at
transcription time; the only network event is the initial public model
download, cached under `.cache/models/whisper/` (gitignored).

## Scope and ownership

- Owns: `simulation/local_stt.py`, `simulation/tests/test_local_stt.py`, this doc.
- Does not touch: backend API, models, service, viewer, or UI. Root wires
  the adapter into the runtime.

## Quick start

```bash
# CLI (uses .cache/dimos/.venv, which has faster-whisper)
.cache/dimos/.venv/bin/python -m simulation.local_stt \
    .data/simulation/speech/<utterance-uuid>.wav --utterance-id <utterance-uuid>
```

Python adapter:

```python
from simulation.local_stt import LocalSTTAdapter

adapter = LocalSTTAdapter()          # tiny.en, CPU, int8, .cache/models/whisper
result = adapter.transcribe(path_or_bytes, utterance_id=stable_id)
print(result.text, result.latency_ms)
for seg in result.segments:
    print(seg.start, seg.end, seg.text, seg.avg_logprob, seg.no_speech_prob)
```

`transcribe()` accepts a filesystem path (str or `Path`) or raw WAV bytes.
`utterance_id` is the caller's stable identifier and is passed through
unchanged on the result.

## Audio bounds

Same envelope as `robot_backend/app/brain/audio.py` so a clip valid there is
valid here: 16-bit PCM WAV, mono or stereo, 8,000-48,000 Hz, 1 frame to 20 s,
4,000,000 decoded bytes max. The simulation speech clips (22.05 kHz mono) fit.
Rejected audio raises `LocalSTTError` with a bounded message.

## Language and confidence

Language is forced to English (`language="en"`); no detection is performed.
Per-segment `avg_logprob` and `no_speech_prob` are raw Whisper outputs,
passed through unchanged. They are NOT calibrated confidence estimates: they
have not been evaluated against ground truth on this robot. Output text never
carries an invented confidence value.

## Performance (measured, Apple Silicon CPU, tiny.en int8)

Model load from an OS-warm cache: ~0.55 s (first-ever load, including the
model download and CTranslate2 conversion, was ~10 s). Warm transcribe
latency across six real clips, measured in-process:

| Clip | Text out | Latency |
|---|---|---|
| Hi I am Annie (5.27 s audio) | "Hi, I am Annie, I can walk around the house, look through my camera, and speak with you." | 209 ms |
| okay | "Okay." | 121 ms |
| help me | "Help me." | 120 ms |
| not okay | "Not okay." | 124 ms |
| okay help | "Okay, help." | 120 ms |
| silence | "" (no segments) | 115 ms |

Goal of <2 s warm latency is met with wide margin. Raw numbers are also
captured in `.data/simulation/speech/stt_benchmark.json` (gitignored;
`.data/` is not tracked).

## Tests

`simulation/tests/test_local_stt.py` always mocks the model (no network in
routine tests). Run with:

```bash
.cache/dimos/.venv/bin/python -m pytest simulation/tests/test_local_stt.py -q
```

The benchmark above was a separate, one-off real-model run, not part of the
test suite.

## Silence behavior

A fully silent clip yields empty text and no segments (`segments == []`).
Callers must treat empty `text` as "no intelligible speech", never as a
command or an error.
