//
//  AppState.swift
//  AnnieApp
//
//  Observable state for the whole app. Loads from the live backend when it's
//  reachable and falls back to bundled demo data when it isn't, so a crashed
//  backend mid-demo never blanks the screen.
//

import AVFoundation
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published var live = false
    @Published var reminders: [Reminder] = []
    @Published var memory: [MemoryFact] = []
    @Published var answer: String?
    @Published var asking = false

    @Published private(set) var serverURL = AppConfiguration.apiBaseURL

    private var api = AnnieAPI()
    private var pollTask: Task<Void, Never>?
    private let synthesizer = AVSpeechSynthesizer()

    // MARK: Lifecycle

    /// Point the app at a different backend (the Profile tab's server field)
    /// and reconnect. Returns false, changing nothing, if the text isn't a
    /// usable address.
    func setServer(_ text: String) async -> Bool {
        guard let url = AppConfiguration.saveAPIBaseURL(text) else { return false }
        serverURL = url
        api = AnnieAPI(baseURL: url)
        await load()
        return true
    }

    /// Retry the backend when the app returns to the foreground while offline
    /// (a phone that slept, or a backend started after the app).
    func reconnectIfOffline() async {
        guard !live else { return }
        await load()
    }

    func load() async {
        do {
            reminders = try await api.reminders()
            memory = try await api.memory()
            live = true
            startPolling()
        } catch {
            live = false
            reminders = Self.demoReminders
            memory = Self.demoMemory
        }
    }

    /// Pick up perception events the dog writes to memory while we're live.
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                await self?.refreshMemory()
            }
        }
    }

    private func refreshMemory() async {
        guard live, let fresh = try? await api.memory() else { return }
        memory = fresh
    }

    // MARK: Reminders

    func toggle(_ reminder: Reminder) async {
        guard let index = reminders.firstIndex(where: { $0.id == reminder.id }) else { return }
        reminders[index].done.toggle()
        guard live else { return }
        do {
            reminders[index] = try await api.toggleReminder(id: reminder.id)
        } catch {
            reminders[index].done.toggle()  // backend dropped; undo optimistic flip
            live = false
        }
    }

    func add(time: String, title: String) async {
        guard live else {
            reminders.append(Reminder(id: 900 + reminders.count, time: time, title: title, done: false))
            sortReminders()
            return
        }
        do {
            reminders.append(try await api.addReminder(NewReminder(time: time, title: title)))
            sortReminders()
        } catch {
            live = false
        }
    }

    private func sortReminders() {
        reminders.sort { $0.time < $1.time }
    }

    // MARK: Ask Annie

    func ask(_ question: String) async {
        asking = true
        defer { asking = false }
        guard live else {
            answer = Self.demoAnswer(question, reminders: reminders)
            return
        }
        do {
            answer = try await api.ask(question)
        } catch {
            answer = Self.demoAnswer(question, reminders: reminders)
            live = false
        }
    }

    /// System speech synthesis stands in for ElevenLabs until it's wired in.
    func speak(_ text: String) {
        synthesizer.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = 0.48
        synthesizer.speak(utterance)
    }

    // MARK: Demo fallback
    //
    // Mirrors the backend's seed data and keyword matching, so the app is
    // fully usable with the backend offline.

    static let demoReminders: [Reminder] = [
        Reminder(id: 1, time: "08:00", title: "Take morning medication", done: true),
        Reminder(id: 2, time: "09:30", title: "Morning walk with Annie", done: true),
        Reminder(id: 3, time: "12:30", title: "Take midday medication", done: false),
        Reminder(id: 4, time: "15:00", title: "Physical therapy exercises", done: false),
        Reminder(id: 5, time: "18:00", title: "Take evening medication", done: false),
        Reminder(id: 6, time: "20:00", title: "Wind down for bed", done: false),
    ]

    static let demoMemory: [MemoryFact] = [
        MemoryFact(id: 1, subject: "user", relation: "took", object: "morning_medication", room: "kitchen",
                   timestamp: "2026-09-19T08:02:00", text: "Saw you take your morning medication in the kitchen."),
        MemoryFact(id: 2, subject: "glasses", relation: "located_at", object: "kitchen_table", room: "kitchen",
                   timestamp: "2026-09-19T09:15:00", text: "Glasses last seen on the kitchen table."),
        MemoryFact(id: 3, subject: "user", relation: "walked", object: "block", room: "outside",
                   timestamp: "2026-09-19T09:34:00", text: "Walked with you around the block."),
        MemoryFact(id: 4, subject: "dog", relation: "followed", object: "user", room: "living_room",
                   timestamp: "2026-09-19T11:40:00", text: "Followed you to the living room."),
        MemoryFact(id: 5, subject: "door", relation: "opened_by", object: "maya", room: "entryway",
                   timestamp: "2026-09-19T13:05:00", text: "Front door opened \u{2014} Maya's visit logged."),
        MemoryFact(id: 6, subject: "glasses", relation: "located_at", object: "reading_chair", room: "living_room",
                   timestamp: "2026-09-19T14:30:00", text: "Glasses moved to the reading chair, living room."),
    ]

    static func demoAnswer(_ question: String, reminders: [Reminder]) -> String {
        let q = question.lowercased()
        if q.contains("glasses") {
            return "Last I saw, glasses moved to the reading chair, living room, around 2:30 PM."
        }
        if q.contains("medication") || q.contains("pill") {
            let meds = reminders.filter { $0.title.lowercased().contains("medication") }
            return "You've taken \(meds.filter(\.done).count) of \(meds.count) medication reminders today."
        }
        if q.contains("walk") {
            if let walk = reminders.first(where: { $0.title.lowercased().contains("walk") && !$0.done }) {
                return "Your next walk is at \(fmtClock(walk.time))."
            }
            return "Today's walk is already done \u{2014} nicely done!"
        }
        if q.contains("visit") || q.contains("anyone") {
            return "Yes \u{2014} front door opened. Maya's visit was logged."
        }
        return "I don't have anything on that yet, but I'm keeping watch."
    }
}
