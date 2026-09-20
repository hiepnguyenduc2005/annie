import AVFoundation
import os

enum AudioCaptureError: LocalizedError {
    case noInputAvailable
    case converterUnavailable

    var errorDescription: String? {
        switch self {
        case .noInputAvailable: return "No microphone input is available."
        case .converterUnavailable: return "Cannot convert microphone audio to 16 kHz PCM."
        }
    }
}

/// Captures the microphone through the shared engine's input node and emits
/// fixed-size packets of wire-format PCM (see `NetworkAudioFormat`).
///
/// Nothing is written to disk; audio only exists in memory until it is handed
/// to `onPacket`.
final class AudioCapture {
    /// Called on the audio thread with exactly `NetworkAudioFormat.bytesPerPacket`
    /// bytes. Must return quickly.
    var onPacket: ((Data) -> Void)?

    private static let log = Logger(subsystem: "app.annie.audio", category: "capture")

    private weak var engine: AVAudioEngine?
    private var converter: AVAudioConverter?
    /// Converted bytes not yet forming a whole packet. Only touched from the
    /// tap callback (and after the tap is removed).
    private var pending = Data()

    // MARK: Permission

    static var permissionGranted: Bool { AVAudioApplication.shared.recordPermission == .granted }

    /// Prompts the first time; afterwards returns the remembered answer.
    static func requestPermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    // MARK: Lifecycle

    /// Installs the input tap. Call before the engine starts.
    func prepare(engine: AVAudioEngine) throws {
        let input = engine.inputNode
        // The hardware (or voice-processing) format: often 48 kHz or 24 kHz,
        // possibly multi-channel. Never assume it matches the wire format.
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw AudioCaptureError.noInputAvailable
        }
        guard let converter = AVAudioConverter(from: inputFormat, to: NetworkAudioFormat.int16) else {
            throw AudioCaptureError.converterUnavailable
        }
        self.engine = engine
        self.converter = converter
        pending.removeAll(keepingCapacity: true)

        // The tap may deliver larger/smaller buffers than requested; packets
        // are re-cut to a fixed size in `process`.
        input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, _ in
            self?.process(buffer)
        }
        Self.log.info("Capture from \(inputFormat.sampleRate, format: .fixed(precision: 0)) Hz, \(inputFormat.channelCount) ch")
    }

    func stop() {
        engine?.inputNode.removeTap(onBus: 0)
        engine = nil
        converter = nil
        pending.removeAll()
    }

    // MARK: Conversion

    private func process(_ input: AVAudioPCMBuffer) {
        guard let converter else { return }

        let ratio = NetworkAudioFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount((Double(input.frameLength) * ratio).rounded(.up)) + 32
        guard let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else { return }

        // Hand the converter this buffer once, then report "no more data right
        // now" (not end-of-stream) so its resampler state carries into the
        // next callback without clicks.
        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            if supplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return input
        }
        guard status != .error, output.frameLength > 0, let samples = output.int16ChannelData?[0] else {
            if let conversionError { Self.log.error("Conversion failed: \(conversionError.localizedDescription)") }
            return
        }

        pending.append(contentsOf: UnsafeRawBufferPointer(
            start: samples, count: Int(output.frameLength) * NetworkAudioFormat.bytesPerSample))

        let packetSize = NetworkAudioFormat.bytesPerPacket
        while pending.count >= packetSize {
            onPacket?(Data(pending.prefix(packetSize)))
            pending.removeFirst(packetSize)
        }
    }
}
