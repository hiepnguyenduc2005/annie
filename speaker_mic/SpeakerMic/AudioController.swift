import AVFoundation
import Combine
import os
import UIKit

/// One line of status in the UI.
struct StatusLine: Equatable {
    enum Level { case off, ok, warning, error }
    var text: String
    var level: Level
}

/// Wires capture, playback and the WebSocket together and exposes their
/// status to SwiftUI. Contains no audio processing of its own: the phone is
/// only a network microphone and speaker.
///
///     mic -> AudioCapture -> AudioConnection -> backend
///     backend -> AudioConnection -> AudioPlayback -> speaker
@MainActor
final class AudioController: ObservableObject {
    let connection = AudioConnection()

    @Published private(set) var isRunning = false
    @Published private(set) var isStarting = false
    @Published private(set) var microphone = StatusLine(text: "Off", level: .off)
    @Published private(set) var speaker = StatusLine(text: "Off", level: .off)
    @Published private(set) var microphoneDenied = false

    private static let log = Logger(subsystem: "app.annie.audio", category: "controller")

    private let host = AudioEngineHost()
    private let capture = AudioCapture()
    private let playback = AudioPlayback()
    private var cancellables = Set<AnyCancellable>()
    private var observers: [NSObjectProtocol] = []
    private var interrupted = false
    private var recentRestarts: [Date] = []
    private var playbackTurnID: String?
    private var expectedReplyBytes = 0
    private var receivedReplyBytes = 0

    init() {
        // Re-publish connection changes so one observed object drives the UI.
        connection.objectWillChange
            .sink { [weak self] _ in self?.objectWillChange.send() }
            .store(in: &cancellables)

        connection.onAudioReceived = { [weak self] data in
            guard let self, self.isRunning, self.playbackTurnID != nil else { return }
            guard data.count % 2 == 0, self.receivedReplyBytes + data.count <= self.expectedReplyBytes else {
                self.stopAudio()
                self.speaker = StatusLine(text: "Invalid reply audio", level: .error)
                return
            }
            self.receivedReplyBytes += data.count
            self.playback.enqueue(data)
        }
        connection.onControlReceived = { [weak self] event in self?.handleControl(event) }
        connection.onConnectionLost = { [weak self] in
            self?.playback.clear()
            self?.playbackTurnID = nil
        }

        // Audio thread -> main queue (FIFO, so packet order is preserved).
        capture.onPacket = { [weak connection] data in
            DispatchQueue.main.async {
                MainActor.assumeIsolated { connection?.sendAudio(data) }
            }
        }
        playback.onPlayingChange = { [weak self] playing in
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.playbackChanged(playing: playing) }
            }
        }
        observeSystemEvents()
    }

    var serverStatus: StatusLine {
        switch connection.state {
        case .disconnected: return StatusLine(text: "Disconnected", level: .off)
        case .connecting: return StatusLine(text: "Connecting…", level: .warning)
        case .connected: return StatusLine(text: "Connected", level: .ok)
        case .reconnecting(let attempt): return StatusLine(text: "Reconnecting (\(attempt))…", level: .warning)
        }
    }

    var isConnectionActive: Bool { connection.state != .disconnected }

    var conversationStatus: StatusLine {
        let value = connection.conversationState
        switch value {
        case "listening": return StatusLine(text: "Listening", level: .ok)
        case "thinking": return StatusLine(text: "Thinking…", level: .warning)
        case "speaking": return StatusLine(text: "Speaking", level: .ok)
        default: return StatusLine(text: "Stopped", level: .off)
        }
    }

    // MARK: Actions

    func toggleConnection() {
        if isConnectionActive {
            stopAudio()
            connection.disconnect()
        } else { connection.connect() }
    }

    func toggleAudio() {
        if isRunning { stopAudio() } else { Task { await startAudio() } }
    }

    func startAudio() async {
        guard !isRunning, !isStarting else { return }
        isStarting = true
        defer { isStarting = false }

        let granted = await AudioCapture.requestPermission()
        microphoneDenied = !granted

        do {
            // Without permission the session still plays audio, just no mic.
            let engine = try host.prepare(withInput: granted)
            playback.prepare(engine: engine)
            if granted { try capture.prepare(engine: engine) }
            try host.run()
            playback.start()
        } catch {
            Self.log.error("Audio start failed: \(error.localizedDescription)")
            teardownAudio()
            microphone = StatusLine(text: "Error", level: .error)
            speaker = StatusLine(text: error.localizedDescription, level: .error)
            return
        }

        isRunning = true
        UIApplication.shared.isIdleTimerDisabled = true   // don't sleep mid-stream
        if !granted {
            microphone = StatusLine(text: "Permission denied", level: .error)
        } else if host.voiceProcessingActive {
            microphone = StatusLine(text: "Active", level: .ok)
        } else {
            microphone = StatusLine(text: "Active (no echo cancellation)", level: .warning)
        }
        speaker = StatusLine(text: "Ready", level: .ok)
        connection.startConversation()
    }

    func stopAudio() {
        connection.stopConversation()
        playbackTurnID = nil
        teardownAudio()
        isRunning = false
        UIApplication.shared.isIdleTimerDisabled = false
        microphone = StatusLine(text: "Off", level: .off)
        speaker = StatusLine(text: "Off", level: .off)
    }

    private func teardownAudio() {
        capture.stop()
        playback.stop()
        host.stop()
    }

    private func playbackChanged(playing: Bool) {
        guard isRunning else { return }
        Self.log.debug("Playback \(playing ? "started" : "finished")")
        speaker = StatusLine(text: playing ? "Playing" : "Ready", level: .ok)
    }

    private func handleControl(_ event: AudioServerEvent) {
        switch event.type {
        case "audio_start":
            guard isRunning, event.sampleRate == 16000, let count = event.bytes,
                  count > 0, count <= 16000 * 2 * 90, count % 2 == 0,
                  let turnID = event.turnID else {
                stopAudio()
                speaker = StatusLine(text: "Unsupported reply audio", level: .error)
                return
            }
            playback.clear()
            playbackTurnID = turnID
            expectedReplyBytes = count
            receivedReplyBytes = 0
        case "audio_end":
            guard let turnID = playbackTurnID, event.turnID == turnID,
                  receivedReplyBytes == expectedReplyBytes else { return }
            playback.finishUtterance { [weak self] in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard let self, self.isRunning, self.playbackTurnID == turnID else { return }
                        self.playbackTurnID = nil
                        self.connection.acknowledgePlayback(turnID: turnID)
                    }
                }
            }
        case "state" where event.state == "stopped":
            if isRunning {
                // Server stopped after an error/timeout; don't send another stop.
                teardownAudio()
                isRunning = false
                UIApplication.shared.isIdleTimerDisabled = false
                playbackTurnID = nil
                microphone = StatusLine(text: "Off", level: .off)
                speaker = StatusLine(text: "Off", level: .off)
            }
        default: break
        }
    }

    // MARK: System events

    private func observeSystemEvents() {
        let center = NotificationCenter.default

        // Route change (headphones, Bluetooth) or format change: the engine
        // stops itself; rebuild the whole pipeline.
        observers.append(center.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] note in
            MainActor.assumeIsolated {
                guard let self, self.isRunning, (note.object as AnyObject?) === self.host.engine else { return }
                Self.log.notice("Engine configuration changed (engine running: \(self.host.engine.isRunning), voice processing: \(self.host.voiceProcessingActive))")
                self.restartAudio()
            }
        })

        // Phone call, Siri, another app taking the audio session.
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            MainActor.assumeIsolated {
                guard let self, let raw, let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
                switch type {
                case .began:
                    guard self.isRunning else { return }
                    self.stopAudio()
                    self.interrupted = true
                    self.microphone = StatusLine(text: "Interrupted", level: .warning)
                case .ended:
                    guard self.interrupted else { return }
                    self.interrupted = false
                    Task { await self.startAudio() }
                @unknown default:
                    break
                }
            }
        })

        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.isRunning else { return }
                self.restartAudio()
            }
        })
    }

    private func restartAudio() {
        // The engine can stop itself right after starting (e.g. voice
        // processing unsupported). Retry once without it, then give up rather
        // than loop forever.
        let now = Date()
        recentRestarts = recentRestarts.filter { now.timeIntervalSince($0) < 10 } + [now]
        if recentRestarts.count == 2, host.voiceProcessingActive {
            Self.log.notice("Engine keeps stopping; retrying without voice processing")
            host.disableVoiceProcessing()
        } else if recentRestarts.count > 3 {
            stopAudio()
            microphone = StatusLine(text: "Error", level: .error)
            speaker = StatusLine(text: "Audio keeps stopping. Tap Start Audio to retry.", level: .error)
            recentRestarts.removeAll()
            return
        }
        stopAudio()
        Task {
            try? await Task.sleep(for: .milliseconds(300))
            await startAudio()
        }
    }
}
