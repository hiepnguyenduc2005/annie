import AVFoundation

/// Plays wire-format PCM (see `NetworkAudioFormat`) through the shared
/// engine's `AVAudioPlayerNode` as it arrives.
///
/// The engine and player node stay alive for the whole run; each received
/// chunk is turned into a small buffer and scheduled back-to-back behind the
/// previous one, so playback starts on the first chunk and never waits for a
/// complete utterance. If chunks arrive faster than real time they queue; if
/// the network stalls the player simply goes quiet until the next chunk.
final class AudioPlayback {
    /// Called (on an arbitrary queue) when audio starts or finishes playing.
    var onPlayingChange: ((Bool) -> Void)?

    private let queue = DispatchQueue(label: "app.annie.audio.playback")
    private var player: AVAudioPlayerNode?
    /// Guards against completion callbacks of a previous run touching the
    /// counters of the current one.
    private var generation = 0
    private var queuedBuffers = 0
    /// Odd trailing byte of the previous chunk (samples are 2 bytes).
    private var carryByte: UInt8?
    private var drainCompletion: (() -> Void)?

    /// Adds the player node to `engine`. Call before the engine starts.
    func prepare(engine: AVAudioEngine) {
        let node = AVAudioPlayerNode()
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: NetworkAudioFormat.float32)
        queue.sync {
            generation += 1
            player = node
            queuedBuffers = 0
            carryByte = nil
            drainCompletion = nil
        }
    }

    /// Call after the engine has started.
    func start() {
        queue.sync { player?.play() }
    }

    func stop() {
        queue.sync {
            generation += 1
            player?.stop()
            player = nil
            queuedBuffers = 0
            carryByte = nil
            drainCompletion = nil
        }
    }

    /// Drop unplayed audio after a disconnect or cancelled turn.
    func clear() {
        queue.sync {
            generation += 1
            player?.stop()
            player?.play()
            queuedBuffers = 0
            carryByte = nil
            drainCompletion = nil
        }
    }

    /// Called after the server's audio_end, after all enqueue calls. The serial
    /// queue prevents an early acknowledgment while buffers await scheduling.
    func finishUtterance(_ completion: @escaping () -> Void) {
        queue.async { [self] in
            drainCompletion = completion
            notifyDrained()
        }
    }

    private func notifyDrained() {
        guard queuedBuffers == 0, let completion = drainCompletion else { return }
        drainCompletion = nil
        completion()
    }

    /// Queues one received binary frame. Safe to call from any thread.
    func enqueue(_ data: Data) {
        queue.async { [self] in
            guard let player, player.engine?.isRunning == true else { return }

            var bytes = data
            if let carry = carryByte {
                bytes.insert(carry, at: bytes.startIndex)
                carryByte = nil
            }
            if bytes.count % NetworkAudioFormat.bytesPerSample != 0 {
                carryByte = bytes.removeLast()
            }
            let frames = bytes.count / NetworkAudioFormat.bytesPerSample
            guard frames > 0,
                  let buffer = AVAudioPCMBuffer(pcmFormat: NetworkAudioFormat.float32,
                                                frameCapacity: AVAudioFrameCount(frames)),
                  let out = buffer.floatChannelData?[0] else { return }
            buffer.frameLength = AVAudioFrameCount(frames)

            // Int16 little-endian -> Float32 in [-1, 1).
            bytes.withUnsafeBytes { raw in
                for i in 0..<frames {
                    let sample = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))
                    out[i] = Float(sample) / 32_768
                }
            }

            queuedBuffers += 1
            if queuedBuffers == 1 { onPlayingChange?(true) }
            let scheduledIn = generation
            player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                self?.queue.async {
                    guard let self, self.generation == scheduledIn else { return }
                    self.queuedBuffers -= 1
                    if self.queuedBuffers == 0 { self.onPlayingChange?(false) }
                    self.notifyDrained()
                }
            }
        }
    }
}
