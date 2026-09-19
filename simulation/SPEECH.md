# Simulation speech output (offline)

The simulated dog speaks through the macOS built-in `say` binary. This is
fully offline, free, and replaceable later by Deepgram, ElevenLabs, or MiniMax
omni adapters with the same small interface.

## What it does

`simulation/speech.py` exposes `SpeechAdapter.speak(text, command_id)`:

- Accepts at most 2000 characters of non-empty text.
- Rejects malformed command ids (must be a canonical UUID string).
- Renders `{command_id}.wav` under `.data/simulation/speech/` using
  `say --file-format=WAVE --data-format=LEI16@22050 -o <temp> -- <text>`.
  The `--` option terminator (verified against macOS `say`) ensures
  user text beginning with `-` such as `-f/etc/hosts` is spoken literally
  instead of making `say` read that file.
- Runs through `asyncio.create_subprocess_exec` (no shell), serialized by an
  asyncio lock so at most one `say` process runs at a time.
- Bounds each run to 30 seconds; on timeout the process is killed and the
  partial temp file is removed.
- Parses and validates the RIFF/WAVE header (PCM, mono, 16-bit, 22050 Hz)
  before reporting success; duration comes from the actual sample count.
- Never plays audio itself and never reads secrets, network state, or provider
  credentials.

## Receipt contract

`speak()` returns a dict shaped like a command receipt plus clip metadata:

```json
{
  "command_id": "uuid-string",
  "status": "synthesized",
  "file_path": ".data/simulation/speech/<uuid>.wav",
  "duration_s": 1.23,
  "sample_rate_hz": 22050,
  "channels": 1,
  "played": false
}
```

`status` is `synthesized` only after the `say` process exited 0 and the
output passed header validation. `played` is always `false` here; a browser
or viewer must fetch the file, play it, and send a separate acknowledgment.
This adapter intentionally does not implement that acknowledgment path.

Failures raise `SpeechError` (empty/oversized text, bad UUID, spawn failure,
timeout, non-zero exit, truncated or wrong-format WAV). No partial file is
left at the final clip path on failure.

Output size is bounded by the 2000-character input cap plus the 30-second
synthesis deadline; generation of such clips completes well inside that
deadline, so no separate WAV byte-size limit is imposed.

## Browser playback (for the owner/integrator)

The bridge layer that polls app commands should, for a queued `say` command:

1. Call `await speech.speak(item["text"], command_id)`.
2. On success, send an `accepted` receipt with the clip path and duration.
3. Serve `.data/simulation/speech/*.wav` over the existing loopback viewer
   and let the browser fetch and play it after a user-visible cue (no
   autoplay). A separate play-acknowledgment endpoint should flip the clip's
   `played` flag before the bridge sends `completed`.

The adapter itself does not serve files or track playback state; that stays in
the viewer/bridge owner's scope. Until that wiring exists, the bridge keeps
failing `say` commands with "Voice playback unavailable in simulation."

## Voice input

Microphone capture, speech-to-text, and turn-taking are separate future work.
They are not part of this adapter and have no code path here.

## Tests

`simulation/tests/test_speech.py` covers:

- empty, whitespace, non-string, and oversized text rejection
- non-UUID command id rejection
- timeout kills the process and cleans up partial files
- non-zero exit fails without leaving a final clip
- concurrent calls serialize (peak concurrent `say` processes = 1)
- one real synthetic render: header-validated WAV with duration within 50 ms
  of the parsed frame count

Run from the repository root:

```sh
.venv/bin/python -m pytest simulation/tests/test_speech.py -q
```
