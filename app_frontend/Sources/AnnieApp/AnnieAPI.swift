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

    init(baseURL: URL = AppConfiguration.apiBaseURL) {
        self.baseURL = baseURL
    }

    private func url(_ path: String) -> URL {
        baseURL.appending(path: path.hasPrefix("/") ? String(path.dropFirst()) : path)
    }

    private func send(_ path: String, method: String, body: Data? = nil) async throws -> Data {
        // Bounded, so an unreachable LAN host falls back to demo data in seconds, not a minute.
        var request = URLRequest(url: url(path), timeoutInterval: 5)
        request.httpMethod = method
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

    private func send(_ path: String, method: String, body: some Encodable) async throws -> Data {
        try await send(path, method: method, body: try JSONEncoder().encode(body))
    }

    // MARK: Reminders

    func reminders() async throws -> [Reminder] {
        try JSONDecoder().decode([Reminder].self, from: try await send("api/reminders", method: "GET"))
    }

    func addReminder(_ new: NewReminder) async throws -> Reminder {
        try JSONDecoder().decode(Reminder.self, from: try await send("api/reminders", method: "POST", body: new))
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
        try JSONDecoder().decode(AskResponse.self, from: try await send("api/ask", method: "POST", body: AskRequest(question: question))).answer
    }
}
