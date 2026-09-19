//
//  main.swift
//  Annie Companion API
//
//  A small, in-memory HTTP server for the Annie aid-dog front end — a Swift
//  port of the FastAPI backend (same routes, same JSON shapes), so the HTML
//  front end can't tell the difference. Zero dependencies: just Foundation
//  and POSIX sockets.
//
//  Two data stores, mirroring the two things the real DimOS integration will
//  eventually feed:
//    - reminders : the scheduling/reminder list (what the app manages directly)
//    - memory    : a simple spatiotemporal fact log (subject, relation, object,
//                  room, timestamp) — the seam where real perception events
//                  from the dog (via DimOS Spatial Memory) get written in,
//                  and where "Ask Annie" queries get answered from.
//
//  Run it (from this directory):
//      swift main.swift
//  or compile once and run the binary:
//      swiftc -O main.swift -o annie-api && ./annie-api
//
//  Then open http://127.0.0.1:8000 — this serves the front end AND the API
//  from the same origin, so there's no CORS setup needed for local dev.
//  (CORS is left wide open for demo convenience, same as the Python version.
//  Tighten this before any real deployment.)
//

import Foundation
import Darwin

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

struct Reminder: Codable {
    var id: Int
    var time: String  // "HH:MM", 24-hour
    var title: String
    var done: Bool = false
}

struct NewReminder: Codable {
    let time: String
    let title: String
}

struct MemoryFact: Codable {
    let id: Int
    let subject: String
    let relation: String
    let object: String
    let room: String
    let timestamp: String  // ISO 8601
    let text: String       // human-readable rendering, shown in the activity feed
}

struct NewMemoryFact: Codable {
    let subject: String
    let relation: String
    let object: String
    let room: String
    let text: String?  // auto-generated from the fields if omitted
}

struct AskRequest: Codable {
    let question: String
}

struct AskResponse: Codable {
    let answer: String
}

struct Detail: Codable {
    let detail: String
}

// ---------------------------------------------------------------------------
// In-memory store (swap for a real DB / DimOS memory client later)
// ---------------------------------------------------------------------------

/// Shared mutable state, guarded by a lock because each connection is handled
/// on its own thread. `@unchecked Sendable` because the lock is the sync story.
final class Store: @unchecked Sendable {
    private let lock = NSLock()

    private(set) var reminders: [Reminder] = [
        Reminder(id: 1, time: "08:00", title: "Take morning medication", done: true),
        Reminder(id: 2, time: "09:30", title: "Morning walk with Annie", done: true),
        Reminder(id: 3, time: "12:30", title: "Take midday medication", done: false),
        Reminder(id: 4, time: "15:00", title: "Physical therapy exercises", done: false),
        Reminder(id: 5, time: "18:00", title: "Take evening medication", done: false),
        Reminder(id: 6, time: "20:00", title: "Wind down for bed", done: false),
    ]

    private(set) var memory: [MemoryFact] = [
        MemoryFact(id: 1, subject: "user", relation: "took", object: "morning_medication",
                   room: "kitchen", timestamp: "2026-09-19T08:02:00",
                   text: "Saw you take your morning medication in the kitchen."),
        MemoryFact(id: 2, subject: "glasses", relation: "located_at", object: "kitchen_table",
                   room: "kitchen", timestamp: "2026-09-19T09:15:00",
                   text: "Glasses last seen on the kitchen table."),
        MemoryFact(id: 3, subject: "user", relation: "walked", object: "block",
                   room: "outside", timestamp: "2026-09-19T09:34:00",
                   text: "Walked with you around the block."),
        MemoryFact(id: 4, subject: "dog", relation: "followed", object: "user",
                   room: "living_room", timestamp: "2026-09-19T11:40:00",
                   text: "Followed you to the living room."),
        MemoryFact(id: 5, subject: "door", relation: "opened_by", object: "maya",
                   room: "entryway", timestamp: "2026-09-19T13:05:00",
                   text: "Front door opened \u{2014} Maya's visit logged."),
        MemoryFact(id: 6, subject: "glasses", relation: "located_at", object: "reading_chair",
                   room: "living_room", timestamp: "2026-09-19T14:30:00",
                   text: "Glasses moved to the reading chair, living room."),
    ]

    private var nextReminderID = 7
    private var nextMemoryID = 7

    // MARK: Reminders

    func listReminders() -> [Reminder] {
        lock.lock(); defer { lock.unlock() }
        return reminders
    }

    func addReminder(_ new: NewReminder) -> Reminder {
        lock.lock(); defer { lock.unlock() }
        let r = Reminder(id: nextReminderID, time: new.time, title: new.title, done: false)
        nextReminderID += 1
        reminders.append(r)
        reminders.sort { $0.time < $1.time }
        return r
    }

    func toggleReminder(_ id: Int) -> Reminder? {
        lock.lock(); defer { lock.unlock() }
        guard let idx = reminders.firstIndex(where: { $0.id == id }) else { return nil }
        reminders[idx].done.toggle()
        return reminders[idx]
    }

    // MARK: Memory (spatiotemporal facts)

    func listMemory() -> [MemoryFact] {
        lock.lock(); defer { lock.unlock() }
        return memory
    }

    /// The write-side endpoint: wire a perception pipeline (or a DimOS Spatial
    /// Memory event callback) here whenever it observes something — an object
    /// detection, a room transition, a person event.
    func addMemory(_ new: NewMemoryFact) -> MemoryFact {
        lock.lock(); defer { lock.unlock() }
        let text = new.text ?? (
            "\(new.subject) \(new.relation.replacingOccurrences(of: "_", with: " ")) "
            + "\(new.object.replacingOccurrences(of: "_", with: " ")) "
            + "in the \(new.room.replacingOccurrences(of: "_", with: " "))."
        )
        let fact = MemoryFact(
            id: nextMemoryID,
            subject: new.subject,
            relation: new.relation,
            object: new.object,
            room: new.room,
            timestamp: isoFormatter.string(from: Date()),
            text: text)
        nextMemoryID += 1
        memory.append(fact)
        return fact
    }

    // MARK: Ask Annie

    /// Keyword-matched for the hackathon demo. This is the seam to swap in a
    /// real query against DimOS Spatial Memory (spatio-temporal RAG) once
    /// that's wired up — same request/response shape, different internals.
    func ask(_ question: String) -> String {
        lock.lock(); defer { lock.unlock() }
        let q = question.lowercased()

        if q.contains("glasses") {
            if let fact = findLatest("glasses") {
                let when = (fact.timestamp.contains("T") && fact.timestamp.count >= 16)
                    ? String(fact.timestamp.dropFirst(11).prefix(5))
                    : fact.timestamp
                var text = fact.text
                while text.hasSuffix(".") { text.removeLast() }
                return "Last I saw, \(text.lowercased()), around \(when)."
            }
            return "I haven't spotted your glasses yet today."
        }

        if q.contains("medication") || q.contains("pill") {
            let meds = reminders.filter { $0.title.lowercased().contains("medication") }
            let taken = meds.filter(\.done).count
            return "You've taken \(taken) of \(meds.count) medication reminders today."
        }

        if q.contains("walk") {
            if let walk = reminders.first(where: { $0.title.lowercased().contains("walk") && !$0.done }) {
                return "Your next walk is at \(walk.time)."
            }
            return "Today's walk is already done \u{2014} nicely done!"
        }

        if q.contains("visit") || q.contains("anyone") {
            if let fact = findLatest("maya") ?? findLatest("door") ?? findLatest("visit") {
                return "Yes \u{2014} \(fact.text.prefix(1).lowercased())\(fact.text.dropFirst())"
            }
            return "No visitors logged yet today."
        }

        return "I don't have anything on that yet, but I'm keeping watch."
    }

    /// Latest fact mentioning `keyword` in subject, object, or text.
    private func findLatest(_ keyword: String) -> MemoryFact? {
        for fact in memory.reversed() {
            let haystack = "\(fact.subject) \(fact.object) \(fact.text)".lowercased()
            if haystack.contains(keyword) {
                return fact
            }
        }
        return nil
    }

    private let isoFormatter = ISO8601DateFormatter()
}

// ---------------------------------------------------------------------------
// HTTP plumbing (minimal HTTP/1.1, one request per connection)
// ---------------------------------------------------------------------------

struct HTTPRequest: Sendable {
    let method: String
    let path: String  // path only, query string stripped
    let body: Data
}

struct HTTPResponse: Sendable {
    let status: Int
    let reason: String
    let contentType: String
    let body: Data

    static func json<T: Encodable>(_ value: T, status: Int = 200) -> HTTPResponse {
        let encoder = JSONEncoder()
        let data = (try? encoder.encode(value)) ?? Data("{}".utf8)
        return HTTPResponse(status: status, reason: reasonPhrase(status),
                            contentType: "application/json", body: data)
    }

    static func error(_ status: Int, _ message: String) -> HTTPResponse {
        .json(Detail(detail: message), status: status)
    }

    private static func reasonPhrase(_ status: Int) -> String {
        switch status {
        case 200: return "OK"
        case 404: return "Not Found"
        case 405: return "Method Not Allowed"
        case 422: return "Unprocessable Entity"
        default: return "Error"
        }
    }
}

/// Read one request: headers first, then Content-Length bytes of body.
func readRequest(fd: Int32) -> HTTPRequest? {
    let headerEnd = Data("\r\n\r\n".utf8)
    var data = Data()
    var chunk = [UInt8](repeating: 0, count: 65536)

    while data.range(of: headerEnd) == nil {
        let n = recv(fd, &chunk, chunk.count, 0)
        if n <= 0 { return nil }
        data.append(contentsOf: chunk[0..<n])
        if data.count > 1_048_576 { return nil }  // don't buffer forever
    }
    guard let headerRange = data.range(of: headerEnd) else { return nil }

    guard let headerBlock = String(data: data[..<headerRange.lowerBound], encoding: .utf8) else {
        return nil
    }
    let lines = headerBlock.components(separatedBy: "\r\n")
    guard let requestLine = lines.first?.components(separatedBy: " "), requestLine.count >= 2 else {
        return nil
    }

    var contentLength = 0
    for line in lines.dropFirst() {
        let parts = line.split(separator: ":", maxSplits: 1)
        if parts.count == 2,
           parts[0].trimmingCharacters(in: .whitespaces).lowercased() == "content-length" {
            contentLength = Int(parts[1].trimmingCharacters(in: .whitespaces)) ?? 0
        }
    }

    let bodyStart = headerRange.upperBound
    while data.count - bodyStart < contentLength {
        let n = recv(fd, &chunk, chunk.count, 0)
        if n <= 0 { break }
        data.append(contentsOf: chunk[0..<n])
    }
    let body = data.subdata(in: bodyStart..<min(data.count, bodyStart + contentLength))

    var path = requestLine[1]
    if let queryStart = path.firstIndex(of: "?") {
        path = String(path[..<queryStart])
    }
    return HTTPRequest(method: requestLine[0].uppercased(), path: path, body: body)
}

/// Serialize and send the response, then let the caller close the connection.
func sendResponse(fd: Int32, _ response: HTTPResponse) {
    let head = """
    HTTP/1.1 \(response.status) \(response.reason)\r
    Content-Type: \(response.contentType)\r
    Content-Length: \(response.body.count)\r
    Access-Control-Allow-Origin: *\r
    Access-Control-Allow-Methods: *\r
    Access-Control-Allow-Headers: *\r
    Connection: close\r
    \r

    """
    var payload = Data(head.utf8)
    payload.append(response.body)
    payload.withUnsafeBytes { ptr in
        guard let base = ptr.baseAddress else { return }
        var sent = 0
        while sent < ptr.count {
            let n = send(fd, base.advanced(by: sent), ptr.count - sent, 0)
            if n <= 0 { return }
            sent += n
        }
    }
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

func handle(_ request: HTTPRequest, store: Store, frontendPath: String) -> HTTPResponse {
    // CORS preflight — left open for demo convenience (see header comment).
    if request.method == "OPTIONS" {
        return HTTPResponse(status: 200, reason: "OK", contentType: "text/plain", body: Data())
    }

    let parts = request.path.split(separator: "/").map(String.init)

    // GET / — serve the front end from the same origin.
    if parts.isEmpty && request.method == "GET" {
        if let html = try? Data(contentsOf: URL(fileURLWithPath: frontendPath)) {
            return HTTPResponse(status: 200, reason: "OK",
                                 contentType: "text/html; charset=utf-8", body: html)
        }
        return .error(404, "Front end not found at \(frontendPath) — is the frontend/ folder in place?")
    }

    // /api/reminders
    if parts == ["api", "reminders"] {
        switch request.method {
        case "GET":
            return .json(store.listReminders())
        case "POST":
            guard let new = try? JSONDecoder().decode(NewReminder.self, from: request.body) else {
                return .error(422, "Expected {\"time\", \"title\"}")
            }
            return .json(store.addReminder(new))
        default:
            return .error(405, "Method not allowed")
        }
    }

    // /api/reminders/{id}/toggle
    if parts.count == 4, parts[0] == "api", parts[1] == "reminders", parts[3] == "toggle" {
        guard request.method == "PATCH" else { return .error(405, "Method not allowed") }
        guard let id = Int(parts[2]) else { return .error(404, "Reminder not found") }
        guard let reminder = store.toggleReminder(id) else {
            return .error(404, "Reminder not found")
        }
        return .json(reminder)
    }

    // /api/memory
    if parts == ["api", "memory"] {
        switch request.method {
        case "GET":
            return .json(store.listMemory())
        case "POST":
            guard let new = try? JSONDecoder().decode(NewMemoryFact.self, from: request.body) else {
                return .error(422, "Expected {\"subject\", \"relation\", \"object\", \"room\"}")
            }
            return .json(store.addMemory(new))
        default:
            return .error(405, "Method not allowed")
        }
    }

    // /api/ask
    if parts == ["api", "ask"] {
        guard request.method == "POST" else { return .error(405, "Method not allowed") }
        guard let req = try? JSONDecoder().decode(AskRequest.self, from: request.body) else {
            return .error(422, "Expected {\"question\"}")
        }
        return .json(AskResponse(answer: store.ask(req.question)))
    }

    return .error(404, "Not Found")
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

signal(SIGPIPE, SIG_IGN)  // a client hanging up mid-write shouldn't kill us

let port: UInt16 = 8000
let store = Store()
let frontendPath = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    .deletingLastPathComponent()  // up out of backend/
    .appendingPathComponent("frontend/annie-companion-app.html")
    .path

let serverFD = socket(AF_INET, SOCK_STREAM, 0)
guard serverFD >= 0 else { fatalError("socket() failed") }

var yes: Int32 = 1
setsockopt(serverFD, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))

var addr = sockaddr_in()
addr.sin_family = sa_family_t(AF_INET)
addr.sin_port = port.bigEndian
addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
addr.sin_addr = in_addr(s_addr: INADDR_ANY)

let bindResult = withUnsafePointer(to: &addr) {
    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        bind(serverFD, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
    }
}
guard bindResult == 0 else { fatalError("bind() failed — is port \(port) already in use?") }
guard listen(serverFD, 128) == 0 else { fatalError("listen() failed") }

print("Annie Companion API listening on http://127.0.0.1:\(port)")

while true {
    let clientFD = accept(serverFD, nil, nil)
    guard clientFD >= 0 else { continue }
    let storeRef = store
    let frontend = frontendPath
    Thread.detachNewThread {
        defer { close(clientFD) }
        guard let request = readRequest(fd: clientFD) else { return }
        let response = handle(request, store: storeRef, frontendPath: frontend)
        sendResponse(fd: clientFD, response)
    }
}
