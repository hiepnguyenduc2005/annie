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

    // The dog itself: live status for the Controls card (nil until the first
    // answer), and the outcome of the last button pressed.
    @Published private(set) var dog: DogStatus?
    @Published private(set) var commandInFlight: DogAction?
    @Published var commandNote: CommandNote?

    // How Annie speaks and hears (Profile > Settings).
    @Published private(set) var voice: VoiceSettings?
    @Published var voiceNote: CommandNote?
    @Published private(set) var voiceSaving = false

    /// A short line of feedback under a control: what happened, in words.
    struct CommandNote: Equatable {
        let text: String
        let isError: Bool
    }

    private var api = AnnieAPI()
    private var pollTask: Task<Void, Never>?
    private var runPollTask: Task<Void, Never>?
    private var dogPollTask: Task<Void, Never>?
    private let synthesizer = AVSpeechSynthesizer()

    /// A run that has said nothing for this long stops being polled; the
    /// errand itself takes 60-90 s, so this is generous without being forever.
    private static let runWatchLimit: TimeInterval = 300

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
            startDogPolling()
            await refreshRuns()
            watchUnfinishedRuns()
        } catch {
            live = false
            dog = nil
            reminders = Self.demoReminders
            memory = Self.demoMemory
        }
    }

    /// Pull-to-refresh on History. Returns once the fresh list is in.
    func refreshHistory() async {
        guard live else {
            await load()
            return
        }
        await refreshMemory()
    }

    /// Pick up perception events the dog writes to memory while we're live.
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)  // History is live: the dog's memory lands within seconds
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

    // MARK: One composer, two paths

    /// Whatever was typed, spoken, or tapped on a chip. A question is answered
    /// instantly from what Annie remembers and never moves the dog; anything
    /// else is an instruction and becomes a mission.
    func submit(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if Self.isQuestion(trimmed) {
            await ask(trimmed)
        } else {
            await send(trimmed)
        }
    }

    /// Ends in "?" or opens with a question word. Deliberately conservative:
    /// "Check on Grandma" and "Tell Grandma..." are instructions.
    static func isQuestion(_ text: String) -> Bool {
        let lowered = text.lowercased()
        if lowered.hasSuffix("?") { return true }
        let openers = ["where", "what", "when", "who", "why", "how", "which", "did", "does", "do", "is", "are",
                       "was", "were", "has", "have", "had", "can", "could", "will", "would", "should"]
        guard let first = lowered.split(whereSeparator: { !$0.isLetter }).first else { return false }
        return openers.contains(String(first))
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
            sendError = "Not connected to Annie. Set the server under Profile, Settings, Advanced."
            return
        }
        do {
            _ = try await api.sendMessage(authorID: authorID, text: trimmed)
            thread = (try? await api.thread()) ?? thread
            await refreshRuns()
            watchUnfinishedRuns()
        } catch {
            sendError = humanMessage(for: error)
            // Only a request that never arrived means the backend is gone. A
            // "busy" or "not understood" answer is the backend working.
            if isTransportFailure(error) { live = false }
        }
    }

    var activeRun: FamilyRun? {
        guard let latest = thread.last else { return nil }
        return runs[latest.run_id]
    }

    func run(for message: ThreadMessage) -> FamilyRun? { runs[message.run_id] }

    /// Total beats across the runs on screen; the conversation scrolls when it grows.
    var visibleBeatCount: Int { runs.values.reduce(0) { $0 + $1.events.count } }

    private func refreshRuns() async {
        // Only the most recent handful matter on screen; older ones stay in
        // whatever state they were last seen in.
        for message in thread.suffix(8) {
            if let run = try? await api.run(id: message.run_id) {
                runs[run.run_id] = run
            }
        }
    }

    /// Poll every recent run that has not reached a terminal state, so beats
    /// appear as the robot reports them — including an earlier errand still
    /// under way when a second message is sent. Stops when all are finished,
    /// or after `runWatchLimit` with no run finishing.
    private func watchUnfinishedRuns() {
        runPollTask?.cancel()
        runPollTask = Task { [weak self] in
            let deadline = Date().addingTimeInterval(Self.runWatchLimit)
            while !Task.isCancelled, Date() < deadline {
                guard let self else { return }
                let pending = self.thread.suffix(8)
                    .map(\.run_id)
                    .filter { !(self.runs[$0]?.finished ?? false) }
                if pending.isEmpty { return }
                for runID in pending {
                    if let run = try? await self.api.run(id: runID) {
                        self.runs[runID] = run
                    }
                }
                try? await Task.sleep(nanoseconds: 800_000_000)
            }
        }
    }

    // MARK: The dog: status and direct controls

    /// The Controls card's status line, refreshed every 3 s while connected.
    private func startDogPolling() {
        dogPollTask?.cancel()
        dogPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshDog()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    private func refreshDog() async {
        guard live else { return }
        // Any failure reads as "dog offline": the card must never show a
        // stale "Following, 63%" for a dog that has stopped answering.
        dog = (try? await api.dogStatus()) ?? .offline
    }

    /// A Controls button. Stop is never blocked behind another command.
    func command(_ action: DogAction) async {
        guard live else {
            commandNote = CommandNote(text: "Not connected to Annie. Set the server under Profile, Settings, Advanced.", isError: true)
            return
        }
        if commandInFlight != nil && action != .stop { return }
        commandInFlight = action
        defer { commandInFlight = nil }
        do {
            try await api.dogCommand(action)
            // Accepted by the dog process. Whether she has actually done it
            // shows up in the status line, not here.
            commandNote = CommandNote(text: action.acknowledgement, isError: false)
            await refreshDog()
        } catch {
            commandNote = CommandNote(text: humanMessage(for: error), isError: true)
            if isTransportFailure(error) { live = false }
        }
    }

    // MARK: Voice settings

    func loadVoiceSettings() async {
        guard live else {
            voice = nil
            return
        }
        voice = (try? await api.voiceSettings()) ?? .unavailable
    }

    /// Send only what changed. Returns true when the dog took the change, so a
    /// caller holding a just-typed key knows whether it was delivered.
    @discardableResult
    func updateVoice(_ update: VoiceSettingsUpdate, saved message: String) async -> Bool {
        guard live else {
            voiceNote = CommandNote(text: "Not connected to Annie. Set the server under Advanced.", isError: true)
            return false
        }
        voiceSaving = true
        defer { voiceSaving = false }
        do {
            voice = try await api.updateVoiceSettings(update)
            voiceNote = CommandNote(text: message, isError: false)
            return true
        } catch {
            voiceNote = CommandNote(text: humanMessage(for: error), isError: true)
            if isTransportFailure(error) { live = false }
            return false
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
