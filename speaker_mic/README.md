# Annie Audio (speaker/mic)

A standalone iPhone app that is only a **network microphone and speaker** for the
robot. It does no speech recognition, synthesis, chat, or robot logic. It is
independent of `app_frontend/` and shares no code with it.

```
voice -> iPhone mic -> AVAudioEngine -> 16 kHz PCM -> WebSocket -> backend
backend -> WebSocket -> PCM -> AVAudioPlayerNode -> iPhone speaker
```

## Wire protocol

One WebSocket (default `ws://<lan-ip>:8080/audio`) carries JSON control
frames and binary PCM: signed 16-bit little-endian, mono, 16,000 Hz.
Phone microphone packets are 20 ms (640 bytes), transmitted only in `listening`.

The server sends `ready` (protocol 1). An idle phone receives
`conversation_requested` with an existing `session_id` when `/requests` queues
work. The phone prepares audio and sends `start` with that ID automatically.
The task stays queued until that acknowledgement, so disconnecting before start
does not consume it. Annie speaks first, then listens for a reply.

Replies use `audio_start` (turn ID, sample rate, byte count), binary PCM, and
`audio_end`. The phone sends `playback_finished` only when playback completes;
the server then enters `listening`. A finished remote conversation stops audio
while leaving the connection available for the next request. Manual Start Audio
still starts a resident-initiated conversation.

The phone reconnects after connection loss. Audio is never written to disk.
The companion backend's v2 dispatch adapter is separate and still pending;
queued family messages do not yet trigger this `/requests` path automatically.

## Run it

1. Open `SpeakerMic.xcodeproj` (iOS 17+). Pick your Team under Signing & Capabilities,
   or create a gitignored `Local.xcconfig` (see `Config.xcconfig`).
2. Connection settings are built into the app; there are no address or key fields.
   Simulator defaults to `ws://127.0.0.1:8080/audio`. For a physical phone, set
   `ANNIE_AUDIO_SERVER_URL = ws:/$()/YOUR-MAC-LAN-IP:8080/audio` in the ignored
   `Local.xcconfig`, then rebuild. The `$()` prevents `//` becoming an xcconfig comment.
   Set `ANNIE_PHONE_API_KEY` there only if the audio backend requires it; this
   development credential is embedded in the installed app, so do not distribute
   that build. Never commit the local configuration.
   Start **robot_backend**, which owns `/audio`, on port 8080; `app_backend` is
   a separate service and has no audio route. `0.0.0.0` is a bind address, not
   the phone's destination. Both devices must be on a reachable network.
3. Run on a device. The app connects automatically while open. Allow microphone
   access when prompted; incoming tasks speak and then listen without tapping
   **Start Audio**. **Disconnect** pauses automatic connection until you reconnect.

Provider-free protocol regression checks run from the repository root:

```sh
.venv/bin/python -m unittest discover -s robot_backend/tests -v
```

`tools/test_server.py` is the older binary-only transport demo; it does not
implement the current conversation controls.

## Layout

| File | Responsibility |
|---|---|
| `AudioFormat.swift` | The wire format constants |
| `AudioEngineHost.swift` | `AVAudioSession` setup and the one shared `AVAudioEngine` |
| `AudioCapture.swift` | Mic tap -> `AVAudioConverter` -> fixed 20 ms PCM packets |
| `AudioPlayback.swift` | PCM chunks -> `AVAudioPlayerNode`, streaming |
| `AudioConnection.swift` | `URLSessionWebSocketTask`, reconnect, status |
| `AudioController.swift` | Wires the above together; interruptions and route changes |
| `ContentView.swift` | The UI |

## Design notes

- **One shared engine.** Apple's echo cancellation (`.playAndRecord` + `.voiceChat`
  + `setVoiceProcessingEnabled`) sits in one I/O unit that sees both mic and speaker
  signals. Separate capture and playback engines would defeat it and the robot would
  hear itself.
- **Voice-processing fallback.** If the engine keeps stopping itself with voice
  processing on (this happens in the iOS Simulator), the app retries once without it
  and shows "Active (no echo cancellation)". It stops retrying after a few failures.
- The screen stays awake while audio runs. There is no background-audio mode, so
  locking the phone or leaving the app ends streaming.
- If microphone permission is denied, the app still plays incoming audio and links to Settings.

## Verified vs. not

Proactive protocol tests use mocked speech and a simulated phone acknowledgement:
idle invitation, reconnect before acceptance, opening speech, reply listening,
and stopping on completion. Simulator compilation checks Swift integration.
Physical audible playback, microphone replies, and hotspot reconnect still need
an on-device check. Background/locked-phone notification delivery is not implemented.
