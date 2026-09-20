import Foundation

@MainActor
final class LiveConnection {
    private var task: Task<Void, Never>?
    private var socket: URLSessionWebSocketTask?

    func stop() {
        task?.cancel()
        task = nil
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
    }

    func start(userID: Int, onEvent: @escaping @MainActor (String) async -> Void,
               onStatus: @escaping @MainActor (Bool) -> Void) {
        stop()
        var components = URLComponents(url: AppConfiguration.apiBaseURL.appendingPathComponent("ws"), resolvingAgainstBaseURL: false)!
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.queryItems = [URLQueryItem(name: "app_user_id", value: String(userID))]
        guard let url = components.url else { return }
        task = Task { [weak self] in
            var delay: UInt64 = 1
            while !Task.isCancelled {
                let socket = URLSession.shared.webSocketTask(with: url)
                self?.socket = socket
                socket.resume()
                let heartbeat = Task {
                    do {
                        while !Task.isCancelled {
                            try await Task.sleep(nanoseconds: 15_000_000_000)
                            let watchdog = Task {
                                try await Task.sleep(nanoseconds: 10_000_000_000)
                                socket.cancel(with: .goingAway, reason: nil)
                            }
                            defer { watchdog.cancel() }
                            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                                socket.sendPing { error in
                                    if let error { continuation.resume(throwing: error) }
                                    else { continuation.resume() }
                                }
                            }
                        }
                    } catch {
                        if !Task.isCancelled { socket.cancel(with: .goingAway, reason: nil) }
                    }
                }
                do {
                    while !Task.isCancelled {
                        let message = try await socket.receive()
                        let data: Data
                        switch message {
                        case .string(let text): data = Data(text.utf8)
                        case .data(let bytes): data = bytes
                        @unknown default: continue
                        }
                        let event = try JSONDecoder().decode(SocketEvent.self, from: data)
                        guard !Task.isCancelled else { break }
                        if event.type == "connected" { onStatus(true); delay = 1 }
                        await onEvent(event.type)
                    }
                } catch {
                    if !Task.isCancelled { onStatus(false) }
                }
                heartbeat.cancel()
                socket.cancel(with: .goingAway, reason: nil)
                guard !Task.isCancelled else { break }
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                delay = min(delay * 2, 30)
            }
        }
    }
}
