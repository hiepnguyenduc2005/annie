import Foundation
import os

struct AudioServerEvent: Decodable {
    let type: String
    let state: String?
    let sessionID: String?
    let turnID: String?
    let sampleRate: Int?
    let bytes: Int?
    let message: String?
    let `protocol`: Int?

    enum CodingKeys: String, CodingKey {
        case type, state, bytes, message, `protocol`
        case sessionID = "session_id"
        case turnID = "turn_id"
        case sampleRate = "sample_rate"
    }
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
/// - Incoming: binary PCM plus JSON session/state/playback controls.
/// - Reconnects with exponential backoff (1 s … 10 s) until `disconnect()`.
/// - Pings every 10 s so a silently dropped Wi-Fi link is noticed.
///
/// Everything runs on the main actor, so the connection state is never
/// touched concurrently; audio threads reach it through `DispatchQueue.main`.
@MainActor
final class AudioConnection: NSObject, ObservableObject {
    @Published private(set) var state: ConnectionState = .disconnected
    @Published private(set) var lastError: String?
    private let serverAddress = Bundle.main.object(forInfoDictionaryKey: "AnnieAudioServerURL") as? String ?? ""
    /// Optional development credential supplied by the local build configuration.
    private let phoneAPIKey = Bundle.main.object(forInfoDictionaryKey: "AnniePhoneAPIKey") as? String ?? ""
    @Published private(set) var conversationState = "stopped"
    @Published private(set) var sessionID: String?

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
    var onControlReceived: ((AudioServerEvent) -> Void)?
    var onConnectionLost: (() -> Void)?
    private var wantsAudio = false
    private var protocolReady = false

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

    var serverURLString: String { serverAddress }

    // MARK: Public API

    func connect() {
        wantsConnection = true
        reconnectAttempt = 0
        reconnectTask?.cancel()
        openSocket()
    }

    func disconnect() {
        wantsConnection = false
        wantsAudio = false
        conversationState = "stopped"
        reconnectTask?.cancel()
        tearDownSocket()
        state = .disconnected
        onConnectionLost?()
    }

    func startConversation() {
        wantsAudio = true
        if state == .connected, protocolReady { sendControl("start", sessionID: sessionID) }
    }

    func stopConversation() {
        wantsAudio = false
        conversationState = "stopped"
        if state == .connected { sendControl("stop") }
        sessionID = nil
    }

    func acknowledgePlayback(turnID: String) {
        sendControl("playback_finished", turnID: turnID)
    }

    private func sendControl(_ type: String, sessionID: String? = nil, turnID: String? = nil) {
        guard let task, state == .connected else { return }
        var body = ["type": type]
        if let sessionID { body["session_id"] = sessionID }
        if let turnID { body["turn_id"] = turnID }
        guard let data = try? JSONSerialization.data(withJSONObject: body),
              let text = String(data: data, encoding: .utf8) else { return }
        let id = generation
        task.send(.string(text)) { [weak self] error in
            guard let error else { return }
            Task { @MainActor in self?.handleFailure(id: id, error: error) }
        }
    }

    /// Sends one microphone packet as a binary frame. Silently drops it if the
    /// socket isn't connected or is backed up (live audio is not worth queueing).
    func sendAudio(_ data: Data) {
        guard state == .connected, wantsAudio, conversationState == "listening", let task else { return }
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

        guard let url = URL(string: serverAddress.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = url.scheme?.lowercased(), scheme == "ws" || scheme == "wss", url.host != nil else {
            wantsConnection = false
            state = .disconnected
            lastError = "Configure ANNIE_AUDIO_SERVER_URL in Local.xcconfig and rebuild."
            return
        }

        let id = generation
        state = reconnectAttempt == 0 ? .connecting : .reconnecting(attempt: reconnectAttempt)
        var request = URLRequest(url: url)
        if !phoneAPIKey.isEmpty { request.setValue("Bearer \(phoneAPIKey)", forHTTPHeaderField: "Authorization") }
        let task = session.webSocketTask(with: request)
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
        conversationState = "stopped"
        protocolReady = false
    }

    /// Called once the WebSocket handshake completes.
    private func handleOpen(_ opened: URLSessionWebSocketTask) {
        guard opened === task else { return }
        state = .connected
        lastError = nil
        reconnectAttempt = 0
        startPinging(task: opened, id: generation)
        if protocolReady, wantsAudio { sendControl("start", sessionID: sessionID) }
        Self.log.info("Connected")
    }

    private func handleFailure(id: Int, error: Error?) {
        guard id == generation else { return }   // stale callback from an old socket
        lastError = error.map { Self.describe($0) } ?? "Connection closed."
        Self.log.error("Connection lost: \(self.lastError ?? "unknown")")
        tearDownSocket()
        onConnectionLost?()

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
                    guard text.utf8.count <= 4096,
                          let event = try? JSONDecoder().decode(AudioServerEvent.self, from: Data(text.utf8)) else {
                        unexpectedTextMessages += 1
                        continue
                    }
                    switch event.type {
                    case "ready":
                        guard event.protocol == 1, event.sampleRate == 16000 else {
                            lastError = "The server uses an unsupported audio protocol."
                            disconnect()
                            return
                        }
                        protocolReady = true
                        if wantsAudio { sendControl("start", sessionID: sessionID) }
                    case "session": sessionID = event.sessionID
                    case "session_ended": sessionID = nil
                    case "state":
                        conversationState = event.state ?? "stopped"
                        if conversationState == "stopped" { wantsAudio = false }
                        if conversationState == "listening" { lastError = nil }
                    case "error": lastError = event.message ?? "Conversation failed."
                    default: break
                    }
                    onControlReceived?(event)
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
                return "Can't reach the server (\(urlError.code.rawValue)). Check the server address, phone API key, and Local Network permission in Settings."
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
