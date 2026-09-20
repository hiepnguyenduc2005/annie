# Annie Audio (speaker/mic)

A standalone iPhone app that is only a **network microphone and speaker** for the
robot. It does no speech recognition, synthesis, chat, or robot logic. It is
independent of `app_frontend/` and shares no code with it.

```
voice -> iPhone mic -> AVAudioEngine -> 16 kHz PCM -> WebSocket -> backend
backend -> WebSocket -> PCM -> AVAudioPlayerNode -> iPhone speaker
```

## Wire protocol

One WebSocket (default `ws://<lan-ip>:8000/audio`). **Binary frames only, both directions:**

| | |
|---|---|
| Encoding | raw PCM, signed 16-bit, little-endian, no header |
| Channels / rate | mono, 16,000 Hz |
| Phone -> backend | 20 ms packets (320 samples = 640 bytes, 50/s) |
| Backend -> phone | any length; played as it arrives (an odd trailing byte is carried to the next frame) |

Text frames are unexpected and ignored. The phone sends a WebSocket ping every 10 s
and reconnects with 1-10 s backoff. If the network backs up (about 1 s queued), new
mic packets are dropped instead of building latency. Audio is never written to disk.

## Run it

1. Open `SpeakerMic.xcodeproj` (iOS 17+). Pick your Team under Signing & Capabilities,
   or create a gitignored `Local.xcconfig` (see `Config.xcconfig`).
2. **Set the backend address** in `SpeakerMic/AudioConnection.swift`
   (`AudioServerConfig.serverURL`). Use the computer's LAN IP, never `127.0.0.1`.
   The phone and computer must share a Wi-Fi network. iOS asks for Local Network
   permission on first connect; plain `ws://` is allowed for local addresses only.
3. Run on a device, tap **Connect**, then **Start Audio** (allow the microphone).

To try it without the real backend:

```
pip install websockets
python tools/test_server.py --mode tone   # phone plays a beep; use --mode echo / sink for other tests
```

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

Verified in the iOS Simulator (Xcode 27) against `tools/test_server.py`: build, connect,
mic capture and conversion (48 kHz -> 16 kHz packets received by the server), playback
scheduling of received audio, and the voice-processing fallback.
**Not verified:** audible playback quality, echo cancellation, and Wi-Fi reconnect
behavior on a physical iPhone.
