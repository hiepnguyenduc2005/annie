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

/// The fallback text both `app_backend`'s `CompanionService.ask` and the demo
/// data use, verbatim, when nothing matches. Not `@MainActor`-isolated so
/// `AskTurn.hasNoAnswer` can read it from any context.
let annieNoAnswerFallback = "I don't have anything on that yet, but I'm keeping watch."

/// One turn of asking Annie something. Answered instantly from what Annie
/// already remembers — never dispatches the robot. Client-side only, like
/// the backend's own lexical recall: nothing here is persisted or synced.
struct AskTurn: Identifiable, Equatable {
    let id = UUID()
    let question: String
    let at: Int
    var answer: String?

    /// True once Annie has answered and the answer is the "nothing on that
    /// yet" fallback — the signal that this needs an in-person check instead
    /// of a lookup.
    var hasNoAnswer: Bool { answer == annieNoAnswerFallback }
}

@MainActor
final class AppState: ObservableObject {
    @Published var live = false
    @Published var reminders: [Reminder] = []
    @Published var memory: [MemoryFact] = []

    // Ask Annie: instant, local, never moves the robot.
    @Published var askTurns: [AskTurn] = []

    // Messages to Annie: dispatches the robot to Jeanine in person, with
    // live progress. Escalating an unanswered ask turn also goes through this.
    @Published var thread: [ThreadMessage] = []
    @Published var runs: [String: FamilyRun] = [:]
    @Published var sending = false
    @Published var sendError: String?

    /// Who this phone is signed in as. Set once at launch from `ProfileState`
    /// and sent as `author_id` on every message.
    @Published var authorID = FamilyMember.zach.rawValue

    @Published private(set) var serverURL = AppConfiguration.apiBaseURL

    private var api = AnnieAPI()
    private var pollTask: Task<Void, Never>?
    private var runPollTask: Task<Void, Never>?
    private let synthesizer = AVSpeechSynthesizer()

    // MARK: Lifecycle

    /// Point the app at a different backend (the Profile tab's server field)
    /// and reconnect. Returns false, changing nothing, if the text isn't a
    /// usable address.
    func setServer(_ text: String, token: String? = nil) async -> Bool {
        guard let url = AppConfiguration.saveAPIBaseURL(text) else { return false }
        if let token { AppConfiguration.saveAPIToken(token) }
        serverURL = url
        api = AnnieAPI(baseURL: url, token: AppConfiguration.apiToken)
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
            thread = (try? await api.thread()) ?? []
            live = true
            startPolling()
            await refreshRuns()
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
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let turn = AskTurn(question: trimmed, at: Int(Date().timeIntervalSince1970 * 1000))
        askTurns.append(turn)
        let index = askTurns.count - 1

        guard live else {
            askTurns[index].answer = Self.demoAnswer(trimmed, reminders: reminders)
            return
        }
        do {
            askTurns[index].answer = try await api.ask(trimmed)
        } catch {
            askTurns[index].answer = Self.demoAnswer(trimmed, reminders: reminders)
            live = false
        }
    }

    // MARK: Messages to Annie

    /// Hand the message to the backend and start watching the run it created.
    /// This returns as soon as the backend accepts it — the dog's errand takes
    /// a minute or more, and the UI must never sit blocked on it.
    func send(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !sending else { return }
        sending = true
        sendError = nil
        defer { sending = false }

        guard live else {
            sendError = "Not connected to Annie. Set the server address in Profile."
            return
        }
        do {
            _ = try await api.sendMessage(authorID: authorID, text: trimmed)
            thread = (try? await api.thread()) ?? thread
            await refreshRuns()
            watchActiveRun()
        } catch {
            sendError = "Couldn't send that. Check the server address in Profile."
            live = false
        }
    }

    var activeRun: FamilyRun? {
        guard let latest = thread.last else { return nil }
        return runs[latest.run_id]
    }

    func run(for message: ThreadMessage) -> FamilyRun? { runs[message.run_id] }

    private func refreshRuns() async {
        // Only the most recent handful matter on screen; older ones stay in
        // whatever state they were last seen in.
        for message in thread.suffix(5) {
            if let run = try? await api.run(id: message.run_id) {
                runs[run.run_id] = run
            }
        }
    }

    /// Poll the newest run until it reaches a terminal state, so the beats
    /// appear as the robot reports them.
    private func watchActiveRun() {
        runPollTask?.cancel()
        guard let runID = thread.last?.run_id else { return }
        runPollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                guard let run = try? await self.api.run(id: runID) else { return }
                self.runs[runID] = run
                if run.finished { return }
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
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
                   timestamp: "2026-09-19T08:02:00", text: "Saw Jeanine take her morning medication in the kitchen."),
        MemoryFact(id: 2, subject: "glasses", relation: "located_at", object: "kitchen_table", room: "kitchen",
                   timestamp: "2026-09-19T09:15:00", text: "Glasses last seen on the kitchen table."),
        MemoryFact(id: 3, subject: "user", relation: "walked", object: "block", room: "outside",
                   timestamp: "2026-09-19T09:34:00", text: "Walked Jeanine around the block."),
        MemoryFact(id: 4, subject: "dog", relation: "followed", object: "user", room: "living_room",
                   timestamp: "2026-09-19T11:40:00", text: "Followed Jeanine to the living room."),
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
            return "Jeanine has taken \(meds.filter(\.done).count) of \(meds.count) medication reminders today."
        }
        if q.contains("walk") {
            if let walk = reminders.first(where: { $0.title.lowercased().contains("walk") && !$0.done }) {
                return "Her next walk is at \(fmtClock(walk.time))."
            }
            return "Today's walk is already done \u{2014} nicely done!"
        }
        if q.contains("visit") || q.contains("anyone") {
            return "Yes \u{2014} front door opened. Maya's visit was logged."
        }
        return annieNoAnswerFallback
    }
}
