import Foundation

enum APIError: Error {
    case badStatus(Int)
    case invalidResponse
}

func connectionMessage(_ error: Error) -> String {
    if case APIError.badStatus(let code) = error {
        switch code {
        case 404: return "This account or record is no longer available. Refresh or select your account again."
        case 409: return "This request conflicts with an earlier request. Please try a new request."
        case 413: return "This conversation has reached its daily limit. Existing messages are still saved."
        case 422: return "The server could not accept these details. Check the fields and try again."
        case 503: return "Annie's database is unavailable. Your last loaded data is shown; please retry."
        default: return "Server error (HTTP \(code)). Please retry."
        }
    }
    if error is DecodingError { return "The server response doesn't match this app version." }
    if case APIError.invalidResponse = error { return "The server response doesn't match this app version." }
    if let error = error as? URLError {
        if error.code == .notConnectedToInternet {
            return "Network unavailable. Check the connection and Annie's Local Network permission."
        }
        if error.code == .cancelled { return "Request cancelled." }
        return "Cannot reach Annie. Check that your Mac and backend are available, then retry."
    }
    return "Couldn't load Annie's data. Please retry."
}

protocol AnnieServing {
    func users() async throws -> [AppUser]
    func residents() async throws -> [DogUser]
    func reminders(userID: Int) async throws -> ReminderPage
    func conversation(userID: Int) async throws -> ConversationPage
    func history(userID: Int, cursor: String?) async throws -> Page<HistoryItem>
    func addReminder(_ body: NewReminder, key: String) async throws -> Reminder
    func sendMessage(_ body: NewMessage, key: String) async throws -> DispatchReceipt
}

struct AnnieAPI: AnnieServing {
    let baseURL: URL
    let session: URLSession

    init(baseURL: URL = AppConfiguration.apiBaseURL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func url(_ path: String, query: [String: String] = [:]) -> URL {
        let base = baseURL.appendingPathComponent(path)
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.queryItems = query.isEmpty ? nil : query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        return components.url!
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try await send(URLRequest(url: url(path, query: query), timeoutInterval: 25))
    }

    private func post<T: Decodable, Body: Encodable>(_ path: String, body: Body, key: String) async throws -> T {
        var request = URLRequest(url: url(path), timeoutInterval: 25)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(key, forHTTPHeaderField: "Idempotency-Key")
        request.httpBody = try JSONEncoder().encode(body)
        return try await send(request)
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard (200..<300).contains(response.statusCode) else { throw APIError.badStatus(response.statusCode) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func all<Item: Decodable>(_ path: String) async throws -> [Item] {
        var items: [Item] = []
        var cursor: String?
        var visited = Set<String>()
        repeat {
            var query = ["limit": "100"]
            if let cursor { query["cursor"] = cursor }
            let page: Page<Item> = try await get(path, query: query)
            items += page.items
            cursor = page.next_cursor
            if let cursor, !visited.insert(cursor).inserted { throw APIError.invalidResponse }
        } while cursor != nil
        return items
    }

    func voiceSettings() async throws -> RemoteVoiceState {
        try await get("api/settings/voice")
    }

    func setMuted(_ muted: Bool) async throws -> RemoteVoiceState {
        try await post("api/settings/voice", body: RemoteVoiceUpdate(muted: muted), key: UUID().uuidString)
    }

    func users() async throws -> [AppUser] { try await all("api/app-users") }
    func residents() async throws -> [DogUser] { try await all("api/dog-users") }

    func reminders(userID: Int) async throws -> ReminderPage {
        var items: [Reminder] = []
        var cursor: String?
        var day: String?
        var visited = Set<String>()
        repeat {
            var query = ["app_user_id": String(userID), "limit": "100"]
            if let cursor { query["cursor"] = cursor }
            if let day { query["day"] = day }
            let page: ReminderPage = try await get("api/reminders", query: query)
            items += page.items
            day = page.day
            cursor = page.next_cursor
            if let cursor, !visited.insert(cursor).inserted { throw APIError.invalidResponse }
        } while cursor != nil
        return ReminderPage(items: items, next_cursor: nil, day: day!)
    }

    func conversation(userID: Int) async throws -> ConversationPage {
        var items: [MessageEntry] = []
        var cursor: String?
        var first: ConversationPage?
        var visited = Set<String>()
        repeat {
            var query = ["app_user_id": String(userID), "limit": "100"]
            if let cursor { query["cursor"] = cursor }
            // The backend chooses today in the resident's timezone on the first page.
            if let first { query["day"] = first.day }
            let page: ConversationPage = try await get("api/messages", query: query)
            if first == nil { first = page }
            items += page.messages
            cursor = page.next_cursor
            if let cursor, !visited.insert(cursor).inserted { throw APIError.invalidResponse }
        } while cursor != nil
        guard let first else { throw APIError.invalidResponse }
        return ConversationPage(id: first.id, app_user_id: first.app_user_id, dog_user_id: first.dog_user_id,
                                day: first.day, messages: items, next_cursor: nil)
    }

    func history(userID: Int, cursor: String?) async throws -> Page<HistoryItem> {
        var query = ["app_user_id": String(userID), "limit": "50"]
        if let cursor { query["cursor"] = cursor }
        return try await get("api/history", query: query)
    }

    func addReminder(_ body: NewReminder, key: String) async throws -> Reminder {
        try await post("api/reminders", body: body, key: key)
    }

    func sendMessage(_ body: NewMessage, key: String) async throws -> DispatchReceipt {
        try await post("api/messages", body: body, key: key)
    }
}
