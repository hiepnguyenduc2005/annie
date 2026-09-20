import Foundation
import os

// MARK: - CONFIGURATION — EDIT THIS

enum AudioServerConfig {
    /// ⚠️ REPLACE THE IP ADDRESS BELOW with the LAN address of the computer
    /// running the audio backend (the phone and that computer must be on the
    /// same Wi-Fi). This is a placeholder, not the final address.
    ///
    /// Do not use 127.0.0.1 / localhost: on the phone that means the phone.
    /// Plain `ws://` is allowed for local-network addresses only (see
    /// NSAllowsLocalNetworking in Info.plist); use `wss://` for anything else.
    static let serverURL = "ws://192.168.1.100:8000/audio"
}

// MARK: -

enum ConnectionState: Equatable {
    case disconnected
    case connecting
    case connected
    case reconnecting(attempt: Int)
}

/// A persistent WebSocket to the audio backend.
///
/// - Outgoing: microphone PCM as binary frames (`sendAudio`).
/// - Incoming: binary frames are PCM for playback (`onAudioReceived`); text
///   frames are unexpected and ignored (counted, never logged verbatim).
/// - Reconnects with exponential backoff (1 s … 10 s) until `disconnect()`.
/// - Pings every 10 s so a silently dropped Wi-Fi link is noticed.
///
/// Everything runs on the main actor, so the connection state is never
/// touched concurrently; audio threads reach it through `DispatchQueue.main`.
@MainActor
final class AudioConnection: NSObject, ObservableObject {
    @Published private(set) var state: ConnectionState = .disconnected
    @Published private(set) var lastError: String?

    // Diagnostics. Deliberately not @Published (they change ~50x/s); the view
    // samples them on a timer.
    private(set) var bytesSent = 0
    private(set) var packetsSent = 0
    private(set) var packetsDropped = 0
    private(set) var bytesReceived = 0
    private(set) var packetsReceived = 0
    private(set) var unexpectedTextMessages = 0

    /// Receives each incoming binary frame (PCM in the wire format).
    var onAudioReceived: ((Data) -> Void)?

    private static let log = Logger(subsystem: "app.annie.audio", category: "connection")
    private static let pingInterval: Duration = .seconds(10)
    private static let maxBackoff: Double = 10
    /// ~1 s of audio. If the network can't keep up, drop new packets rather
    /// than let latency grow without bound.
    private static let maxInFlightSends = 50

    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    /// Bumped whenever a socket is torn down or replaced; async callbacks
    /// carry the value they started with and are ignored if it changed.
    private var generation = 0
    private var wantsConnection = false
    private var reconnectAttempt = 0
    private var inFlightSends = 0
    private var pingOutstanding = false
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?

    override init() {
        super.init()
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5   // handshake / connect timeout
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    var serverURLString: String { AudioServerConfig.serverURL }

    // MARK: Public API

    func connect() {
        wantsConnection = true
        reconnectAttempt = 0
        reconnectTask?.cancel()
        openSocket()
    }

    func disconnect() {
        wantsConnection = false
        reconnectTask?.cancel()
        tearDownSocket()
        state = .disconnected
    }

    /// Sends one microphone packet as a binary frame. Silently drops it if the
    /// socket isn't connected or is backed up (live audio is not worth queueing).
    func sendAudio(_ data: Data) {
        guard state == .connected, let task else { return }
        guard inFlightSends < Self.maxInFlightSends else {
            packetsDropped += 1
            return
        }
        inFlightSends += 1
        bytesSent += data.count
        packetsSent += 1
        let id = generation
        task.send(.data(data)) { [weak self] error in
            Task { @MainActor in
                guard let self, id == self.generation else { return }
                self.inFlightSends -= 1
                if let error { self.handleFailure(id: id, error: error) }
            }
        }
    }

    // MARK: Socket lifecycle

    private func openSocket() {
        tearDownSocket()

        guard let url = URL(string: AudioServerConfig.serverURL),
              let scheme = url.scheme?.lowercased(), scheme == "ws" || scheme == "wss", url.host != nil else {
            wantsConnection = false
            state = .disconnected
            lastError = "Invalid server URL in AudioServerConfig."
            return
        }

        let id = generation
        state = reconnectAttempt == 0 ? .connecting : .reconnecting(attempt: reconnectAttempt)
        let task = session.webSocketTask(with: url)
        task.maximumMessageSize = 4 * 1024 * 1024   // room for bursts of backend audio
        self.task = task
        task.resume()
        receiveTask = Task { [weak self] in await self?.receiveLoop(task: task, id: id) }
    }

    private func tearDownSocket() {
        generation += 1
        receiveTask?.cancel()
        pingTask?.cancel()
        receiveTask = nil
        pingTask = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        inFlightSends = 0
        pingOutstanding = false
    }

    /// Called once the WebSocket handshake completes.
    private func handleOpen(_ opened: URLSessionWebSocketTask) {
        guard opened === task else { return }
        state = .connected
        lastError = nil
        reconnectAttempt = 0
        startPinging(task: opened, id: generation)
        Self.log.info("Connected")
    }

    private func handleFailure(id: Int, error: Error?) {
        guard id == generation else { return }   // stale callback from an old socket
        lastError = error.map { Self.describe($0) } ?? "Connection closed."
        Self.log.error("Connection lost: \(self.lastError ?? "unknown")")
        tearDownSocket()

        guard wantsConnection else {
            state = .disconnected
            return
        }
        reconnectAttempt += 1
        state = .reconnecting(attempt: reconnectAttempt)
        let delay = min(pow(2, Double(reconnectAttempt - 1)), Self.maxBackoff)
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled, let self, self.wantsConnection else { return }
            self.openSocket()
        }
    }

    // MARK: Receiving

    private func receiveLoop(task: URLSessionWebSocketTask, id: Int) async {
        while !Task.isCancelled {
            do {
                let message = try await task.receive()
                guard id == generation else { return }
                switch message {
                case .data(let data):
                    bytesReceived += data.count
                    packetsReceived += 1
                    onAudioReceived?(data)
                case .string(let text):
                    // The protocol is binary-only. Note it, but don't log content.
                    unexpectedTextMessages += 1
                    Self.log.notice("Ignoring unexpected text message (\(text.utf8.count) bytes)")
                @unknown default:
                    break
                }
            } catch {
                handleFailure(id: id, error: error)
                return
            }
        }
    }

    // MARK: Keep-alive

    private func startPinging(task: URLSessionWebSocketTask, id: Int) {
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: Self.pingInterval)
                guard !Task.isCancelled, let self, id == self.generation else { return }
                if self.pingOutstanding {
                    // The previous ping never got a pong: the link is dead.
                    self.handleFailure(id: id, error: URLError(.timedOut))
                    return
                }
                self.pingOutstanding = true
                task.sendPing { [weak self] error in
                    Task { @MainActor in
                        guard let self, id == self.generation else { return }
                        if let error { self.handleFailure(id: id, error: error) } else { self.pingOutstanding = false }
                    }
                }
            }
        }
    }

    private static func describe(_ error: Error) -> String {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cannotConnectToHost, .timedOut, .cannotFindHost, .networkConnectionLost, .notConnectedToInternet:
                return "Can't reach the server (\(urlError.code.rawValue)). Check the IP in AudioServerConfig, that the backend is running, and Local Network permission in Settings."
            default: break
            }
        }
        return error.localizedDescription
    }
}

// MARK: - URLSessionWebSocketDelegate

extension AudioConnection: URLSessionWebSocketDelegate {
    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                                didOpenWithProtocol protocol: String?) {
        Task { @MainActor in self.handleOpen(webSocketTask) }
    }

    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                                didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        Task { @MainActor in
            guard webSocketTask === self.task else { return }
            self.handleFailure(id: self.generation, error: nil)
        }
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error else { return }
        Task { @MainActor in
            guard task === self.task else { return }
            self.handleFailure(id: self.generation, error: error)
        }
    }
}
