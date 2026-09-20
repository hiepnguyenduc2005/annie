//
//  AnnieAPI.swift
//  AnnieApp
//
//  HTTP client for the Annie Companion API. The base URL comes from
//  AppConfiguration — the one place that knows where the server lives.
//

import Foundation

enum APIError: Error {
    case badStatus(Int)
}

struct AnnieAPI {
    var baseURL: URL
    var token: String

    init(baseURL: URL = AppConfiguration.apiBaseURL, token: String = AppConfiguration.apiToken) {
        self.baseURL = baseURL
        self.token = token
    }

    private func url(_ path: String) -> URL {
        baseURL.appending(path: path.hasPrefix("/") ? String(path.dropFirst()) : path)
    }

    private func send(_ path: String, method: String, body: Data? = nil) async throws -> Data {
        // Bounded, so an unreachable LAN host falls back to demo data in seconds, not a minute.
        var request = URLRequest(url: url(path), timeoutInterval: 5)
        request.httpMethod = method
        // app_backend accepts non-loopback clients only with a token, so a
        // phone on the LAN needs this even though the simulator works without.
        if !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIError.badStatus((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        return data
    }

    /// Deliberately NOT an overload of `send`. `Data` is itself `Encodable`, so
    /// a same-named generic sibling resolves its own encoded body back to
    /// itself and recurses forever — and because async frames live on the
    /// heap, it spins silently instead of crashing.
    private func sendJSON(_ path: String, method: String, body: some Encodable) async throws -> Data {
        try await send(path, method: method, body: try JSONEncoder().encode(body))
    }

    // MARK: Reminders

    func reminders() async throws -> [Reminder] {
        try JSONDecoder().decode([Reminder].self, from: try await send("api/reminders", method: "GET"))
    }

    func addReminder(_ new: NewReminder) async throws -> Reminder {
        try JSONDecoder().decode(Reminder.self, from: try await sendJSON("api/reminders", method: "POST", body: new))
    }

    func toggleReminder(id: Int) async throws -> Reminder {
        try JSONDecoder().decode(Reminder.self, from: try await send("api/reminders/\(id)/toggle", method: "PATCH"))
    }

    // MARK: Memory (spatiotemporal facts the dog writes in)

    func memory() async throws -> [MemoryFact] {
        try JSONDecoder().decode([MemoryFact].self, from: try await send("api/memory", method: "GET"))
    }

    // MARK: Ask Annie

    func ask(_ question: String) async throws -> String {
        try JSONDecoder().decode(AskResponse.self, from: try await sendJSON("api/ask", method: "POST", body: AskRequest(question: question))).answer
    }

    // MARK: Family messages
    //
    // Sending returns as soon as the backend has the message; the robot's
    // 60-90 second errand is reported afterwards through the run.

    func sendMessage(authorID: String, text: String) async throws -> DispatchAck {
        let body = NewMessage(author_id: authorID, text: text)
        return try JSONDecoder().decode(DispatchAck.self, from: try await sendJSON("api/messages", method: "POST", body: body))
    }

    func thread() async throws -> [ThreadMessage] {
        try JSONDecoder().decode([ThreadMessage].self, from: try await send("api/thread", method: "GET"))
    }

    func run(id: String) async throws -> FamilyRun {
        try JSONDecoder().decode(FamilyRun.self, from: try await send("api/runs/\(id)", method: "GET"))
    }
}
