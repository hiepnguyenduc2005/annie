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

/// What to tell a family member when a request fails. Status codes and
/// transport errors never reach the screen; this is the one place they are
/// turned into words.
func humanMessage(for error: Error) -> String {
    if case APIError.badStatus(let code) = error {
        switch code {
        case 409: return "Annie is busy right now. Give her a moment and try again."
        case 401, 403: return "This phone isn't signed in to Annie's server. Check the token under Profile, Settings, Advanced."
        case 404: return "Annie's server doesn't know how to do that yet. It may need restarting."
        case 400, 422: return "Annie didn't understand that. Try saying it another way."
        case 502, 503, 504: return "Annie's dog isn't answering right now. Check that she is switched on."
        default: return "Something went wrong on Annie's side. Try again in a moment."
        }
    }
    if let urlError = error as? URLError {
        switch urlError.code {
        case .timedOut: return "Annie is taking too long to answer. Try again in a moment."
        case .notConnectedToInternet, .networkConnectionLost:
            return "This phone is offline. Check Wi-Fi and try again."
        default: return "Can't reach Annie. Check the server under Profile, Settings, Advanced."
        }
    }
    return "Something went wrong. Try again in a moment."
}

/// True when the request never reached the backend (as opposed to the backend
/// answering with an error). Only this should flip the app to demo data.
func isTransportFailure(_ error: Error) -> Bool {
    error is URLError
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

    private func send(_ path: String, method: String, body: Data? = nil, timeout: TimeInterval = 5) async throws -> Data {
        // Bounded, so an unreachable LAN host falls back to demo data in seconds, not a minute.
        var request = URLRequest(url: url(path), timeoutInterval: timeout)
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
    private func sendJSON(_ path: String, method: String, body: some Encodable, timeout: TimeInterval = 5) async throws -> Data {
        try await send(path, method: method, body: try JSONEncoder().encode(body), timeout: timeout)
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

    // MARK: The dog: live status and direct controls

    func dogStatus() async throws -> DogStatus {
        try JSONDecoder().decode(DogStatus.self, from: try await send("api/dog/status", method: "GET"))
    }

    /// One of the Controls buttons. The dog's receipt is not interpreted here;
    /// success means the dog process accepted the command, not that it has
    /// finished (or even started) moving.
    func dogCommand(_ action: DogAction) async throws {
        _ = try await sendJSON("api/dog/command", method: "POST", body: DogCommandBody(action: action.rawValue))
    }

    /// A plain-language instruction, straight to the dog's situated agent. The
    /// answer is the mission board's receipt (accepted or queued); progress and
    /// Annie's reply arrive later in `dogStatus().missions` under the same id.
    func dogInstruct(_ text: String, author: String) async throws -> InstructReceipt {
        let body = DogCommandBody(text: text, author: author)
        // The backend itself waits up to 4 s on the dog before answering.
        return try JSONDecoder().decode(InstructReceipt.self,
                                        from: try await sendJSON("api/dog/command", method: "POST", body: body, timeout: 8))
    }

    // MARK: People Annie knows

    func people() async throws -> PeopleResponse {
        try JSONDecoder().decode(PeopleResponse.self, from: try await send("api/people", method: "GET"))
    }

    /// Enrolling runs the face model over each photo on the dog's machine, so
    /// it gets the same 30 s the backend allows it.
    func addPerson(_ person: NewPerson) async throws -> EnrolResult {
        try JSONDecoder().decode(EnrolResult.self,
                                 from: try await sendJSON("api/people", method: "POST", body: person, timeout: 35))
    }

    func forgetPerson(named name: String) async throws {
        // A name may hold spaces and apostrophes. `URL.appending(path:)` does
        // the percent-encoding itself; encoding here as well would double it
        // ("%20" -> "%2520"). Names never contain "/" (the dog rejects them).
        _ = try await send("api/people/\(name)", method: "DELETE", timeout: 8)
    }

    // MARK: Voice settings

    func voiceSettings() async throws -> VoiceSettings {
        try JSONDecoder().decode(VoiceSettings.self, from: try await send("api/settings/voice", method: "GET"))
    }

    func updateVoiceSettings(_ update: VoiceSettingsUpdate) async throws -> VoiceSettings {
        try JSONDecoder().decode(VoiceSettings.self, from: try await sendJSON("api/settings/voice", method: "POST", body: update))
    }
}
