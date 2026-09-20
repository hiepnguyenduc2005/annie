import Foundation

struct Page<Item: Decodable>: Decodable {
    let items: [Item]
    let next_cursor: String?
}

struct AppUser: Codable, Identifiable, Equatable {
    let id: Int
    let name: String
    let dog_user_id: Int
}

struct DogUser: Codable, Identifiable, Equatable {
    let id: Int
    let name: String
    let timezone: String
}

struct Reminder: Codable, Identifiable, Equatable {
    let id: Int
    let dog_user_id: Int
    let daily_time: String
    let timestamp: String
    let description: String
    let enabled: Bool
    var done: Bool?
    var latest_note: ReminderNote?
}

struct ReminderPage: Decodable {
    let items: [Reminder]
    let next_cursor: String?
    let day: String
}

struct ReminderNote: Codable, Identifiable, Equatable {
    let id: Int
    let reminder_id: Int
    let timestamp: String
    let description: String
    let outcome: String
    let source: String
}

struct NewReminder: Encodable {
    let app_user_id: Int
    let daily_time: String
    let description: String
}

struct MessageEntry: Codable, Identifiable, Equatable {
    let id: Int
    let role: String
    let text: String
    let timestamp: String
    let request_id: Int
    let reply_to: Int?
    var status: String?

    var speaker: String {
        switch role {
        case "app_user": return "You"
        case "resident": return "Resident"
        default: return "Annie"
        }
    }
}

struct ConversationPage: Decodable {
    let id: Int?
    let app_user_id: Int
    let dog_user_id: Int
    let day: String
    let messages: [MessageEntry]
    let next_cursor: String?
}

struct NewMessage: Encodable {
    let app_user_id: Int
    let text: String
}

struct DispatchReceipt: Decodable {
    let conversation_id: Int
    let message_id: Int
    let request_id: Int
    let day: String
    let status: String
}

struct HistoryItem: Codable, Identifiable, Equatable {
    let id: Int
    let kind: String
    let timestamp: String
    let dog_user_id: Int
    let description: String
    let is_emergency: Bool
    let source: String
    let reminder_id: Int?
    let outcome: String?
}

struct SocketEvent: Decodable {
    let type: String
}

func fmtClock(_ value: String) -> String {
    let parts = value.split(separator: ":").compactMap { Int($0) }
    guard parts.count == 2 else { return value }
    return "\((parts[0] + 11) % 12 + 1):\(String(format: "%02d", parts[1])) \(parts[0] < 12 ? "AM" : "PM")"
}

func parseTimestamp(_ value: String) -> Date? {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) { return date }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}

func displayTimestamp(_ value: String, timezone: String, date: Bool = true) -> String {
    guard let parsed = parseTimestamp(value) else { return value }
    let formatter = DateFormatter()
    formatter.timeZone = TimeZone(identifier: timezone)
    formatter.dateStyle = date ? .medium : .none
    formatter.timeStyle = .short
    return formatter.string(from: parsed)
}

func residentDay(timezone: String, now: Date = Date()) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone(identifier: timezone)
    formatter.dateFormat = "yyyy-MM-dd"
    return formatter.string(from: now)
}

func statusLabel(_ status: String?) -> String {
    switch status {
    case "queued": return "Queued for Annie"
    case "sending": return "Sending to Annie"
    case "accepted": return "Accepted by Annie · awaiting reply"
    case "completed": return "Reply received"
    case "failed": return "Request failed"
    default: return ""
    }
}
