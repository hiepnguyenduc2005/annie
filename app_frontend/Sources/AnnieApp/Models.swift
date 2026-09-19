//
//  Models.swift
//  AnnieApp
//
//  Typed contract with the Annie Companion API (backend/main.swift).
//  Field names match the wire format exactly.
//

import Foundation

struct Reminder: Codable, Identifiable, Equatable {
    var id: Int
    var time: String   // "HH:MM", 24-hour
    var title: String
    var done: Bool
}

struct NewReminder: Codable {
    let time: String
    let title: String
}

struct MemoryFact: Codable, Identifiable, Equatable {
    let id: Int
    let subject: String
    let relation: String
    let object: String
    let room: String
    let timestamp: String  // ISO 8601
    let text: String       // human-readable rendering, shown in the activity feed
}

struct AskRequest: Codable {
    let question: String
}

struct AskResponse: Codable {
    let answer: String
}

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------

/// "08:00" -> "8:00 AM" (falls back to the input if it isn't "HH:MM").
func fmtClock(_ hhmm: String) -> String {
    let parts = hhmm.split(separator: ":").compactMap { Int($0) }
    guard parts.count == 2 else { return hhmm }
    let (h, m) = (parts[0], parts[1])
    return "\((h + 11) % 12 + 1):\(String(format: "%02d", m)) \(h >= 12 ? "PM" : "AM")"
}

/// "2026-09-19T08:02:00" -> "8:02 AM" (falls back to the raw string).
func fmtStamp(_ iso: String) -> String {
    guard iso.contains("T"), iso.count >= 16 else { return iso }
    return fmtClock(String(iso.dropFirst(11).prefix(5)))
}
