//
//  SpeechDictation.swift
//  AnnieApp
//
//  The composer's microphone: tap to talk, watch the words land in the text
//  field, tap again to stop. Recognition runs on the phone
//  (`requiresOnDeviceRecognition`) whenever the device supports it, so what a
//  family member says about Jeanine is not sent to a speech service.
//
//  iOS only. The Mac build gets a stub that reports itself unavailable, which
//  hides the button.
//

import SwiftUI

#if os(iOS)
import AVFoundation
import Speech

@MainActor
final class SpeechDictation: ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var transcript = ""
    @Published var problem: String?

    let isSupported = true

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    /// Bumped on every start/stop so a late callback from a finished session
    /// cannot overwrite the transcript of the next one.
    private var session = 0

    func toggle() {
        if isRecording {
            stop()
        } else {
            Task { await start() }
        }
    }

    func start() async {
        guard !isRecording else { return }
        problem = nil

        guard await Self.speechAuthorized() else {
            problem = "Annie needs permission to understand speech. Turn on Speech Recognition for Annie in Settings."
            return
        }
        guard await Self.microphoneAuthorized() else {
            problem = "Annie needs the microphone to hear you. Turn on Microphone for Annie in Settings."
            return
        }
        guard let recognizer, recognizer.isAvailable else {
            problem = "Speech isn't available on this phone right now. You can still type."
            return
        }

        do {
            let audio = AVAudioSession.sharedInstance()
            try audio.setCategory(.playAndRecord, mode: .measurement, options: [.duckOthers, .defaultToSpeaker, .allowBluetooth])
            try audio.setActive(true, options: .notifyOthersOnDeactivation)

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            // Keep the audio on the phone when the device can; older phones
            // fall back to Apple's service rather than having no dictation.
            request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
            self.request = request

            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                throw DictationError.noInput
            }
            input.removeTap(onBus: 0)
            Self.installTap(on: input, format: format, feeding: request)

            engine.prepare()
            try engine.start()

            session += 1
            let current = session
            transcript = ""
            isRecording = true
            task = Self.recognize(request, with: recognizer) { [weak self] text, finished in
                Task { @MainActor in
                    guard let self, self.session == current else { return }
                    if let text { self.transcript = text }
                    if finished { self.stop() }
                }
            }
        } catch {
            teardown()
            problem = "Couldn't start listening. Check that no other app is using the microphone."
        }
    }

    func stop() {
        guard isRecording || task != nil else { return }
        session += 1
        teardown()
    }

    private func teardown() {
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        isRecording = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private enum DictationError: Error { case noInput }

    // The three callbacks below are invoked off the main thread (the audio tap
    // on a real-time thread). They are built in `nonisolated` functions so the
    // closures are not inferred as main-actor-isolated, which would trap at
    // run time the first time the system called them.

    private nonisolated static func speechAuthorized() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    private nonisolated static func microphoneAuthorized() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    private nonisolated static func installTap(on node: AVAudioInputNode, format: AVAudioFormat,
                                               feeding request: SFSpeechAudioBufferRecognitionRequest) {
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            request.append(buffer)
        }
    }

    private nonisolated static func recognize(_ request: SFSpeechAudioBufferRecognitionRequest,
                                              with recognizer: SFSpeechRecognizer,
                                              update: @escaping @Sendable (String?, Bool) -> Void) -> SFSpeechRecognitionTask {
        recognizer.recognitionTask(with: request) { result, error in
            let text = result?.bestTranscription.formattedString
            update(text, error != nil || (result?.isFinal ?? false))
        }
    }
}

#else

/// The Mac build has no dictation; the composer hides the microphone.
@MainActor
final class SpeechDictation: ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var transcript = ""
    @Published var problem: String?

    let isSupported = false

    func toggle() {}
    func start() async {}
    func stop() {}
}

#endif
