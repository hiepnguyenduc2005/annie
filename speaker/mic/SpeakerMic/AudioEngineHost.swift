import AVFoundation
import os

/// Owns the `AVAudioSession` configuration and the single `AVAudioEngine`
/// that `AudioCapture` (input tap) and `AudioPlayback` (player node) both
/// attach to.
///
/// Why one shared engine: Apple's echo cancellation (voice processing) lives
/// in one I/O unit that sees both the microphone signal and what is being
/// played. If capture and playback ran on separate engines, the canceller
/// would not know what the speaker is emitting and the robot would hear
/// itself.
///
/// Echo cancellation notes:
///  * `.playAndRecord` + `.voiceChat` and `setVoiceProcessingEnabled(true)`
///    turn on Apple's built-in acoustic echo cancellation, noise suppression
///    and automatic gain control. No custom DSP is involved.
///  * It works best with the loudspeaker and mic on the same phone, which is
///    the intended setup. It reduces echo; it is not perfect at high volume.
///  * Voice processing lowers/ducks the output and narrows it to a voice
///    band. Set `useVoiceProcessing` to `false` to get plain, louder
///    playback (e.g. when using headphones) at the cost of echo handling.
///  * If the engine keeps stopping itself with voice processing on (seen in
///    the iOS Simulator), `AudioController` calls `disableVoiceProcessing()`
///    and the next run proceeds without echo cancellation.
final class AudioEngineHost {
    static let useVoiceProcessing = true

    private static let log = Logger(subsystem: "app.annie.audio", category: "engine")

    private(set) var engine = AVAudioEngine()
    private(set) var voiceProcessingActive = false
    private var voiceProcessingAllowed = AudioEngineHost.useVoiceProcessing

    /// Configures the session and returns a fresh, stopped engine ready to be
    /// wired. A fresh engine per run avoids stale graph state after route
    /// changes and interruptions.
    ///
    /// - Parameter withInput: `false` when microphone permission was denied;
    ///   the session then only plays audio.
    func prepare(withInput: Bool) throws -> AVAudioEngine {
        let session = AVAudioSession.sharedInstance()
        if withInput {
            // `.defaultToSpeaker`: `.voiceChat` otherwise routes to the quiet
            // earpiece receiver.
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker])
        } else {
            try session.setCategory(.playback, mode: .default)
        }
        // Preferences only; the hardware may pick something else, which is
        // why capture always converts to the wire format.
        try? session.setPreferredSampleRate(NetworkAudioFormat.sampleRate)
        try? session.setPreferredIOBufferDuration(NetworkAudioFormat.packetDuration)
        try session.setActive(true)

        engine = AVAudioEngine()
        voiceProcessingActive = false
        if withInput && voiceProcessingAllowed {
            // Must happen while the engine is stopped and before any node
            // formats are queried.
            do {
                try engine.inputNode.setVoiceProcessingEnabled(true)
                voiceProcessingActive = true
            } catch {
                Self.log.error("Voice processing unavailable, continuing without echo cancellation: \(error.localizedDescription)")
            }
        }
        return engine
    }

    /// Turns voice processing off for all later runs (fallback, see above).
    func disableVoiceProcessing() { voiceProcessingAllowed = false }

    /// Starts the wired engine.
    func run() throws {
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
