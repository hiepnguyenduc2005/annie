import AVFoundation

/// The wire format agreed with the backend, in both directions:
///
///     PCM, signed 16-bit, little-endian, mono, 16,000 Hz, no header
///
/// Each WebSocket binary frame carries raw sample bytes. Frames sent by this
/// app hold exactly `packetDuration` of audio; frames received may be any
/// length (an odd trailing byte is carried over to the next frame).
///
/// All iOS devices are little-endian, so native `Int16` memory *is* the wire
/// format and no byte swapping is needed on the way out.
enum NetworkAudioFormat {
    static let sampleRate: Double = 16_000
    static let channelCount: AVAudioChannelCount = 1
    static let bytesPerSample = MemoryLayout<Int16>.size

    /// Outgoing packet size. 20 ms is the usual low-latency voice frame:
    /// 320 samples = 640 bytes, 50 packets per second.
    static let packetDuration: TimeInterval = 0.020
    static let framesPerPacket = Int(sampleRate * packetDuration)
    static let bytesPerPacket = framesPerPacket * bytesPerSample

    /// Wire format as an `AVAudioFormat` (used as the converter's output).
    static var int16: AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate,
                      channels: channelCount, interleaved: true)!
    }

    /// Same audio as Float32, which is what `AVAudioPlayerNode` schedules; the
    /// engine's mixer resamples it to the hardware rate.
    static var float32: AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
                      channels: channelCount, interleaved: false)!
    }
}
