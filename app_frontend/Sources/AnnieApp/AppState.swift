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

/// An instruction sent straight to the dog (POST /api/dog/command {text}) and
/// what the dog's mission board has said about it since. `state` is the board's
/// word, not an observation: "completed" means Annie reported it done.
struct DirectInstruction: Identifiable, Equatable {
    let id = UUID()
    let text: String
    let at: Int
    var commandID: String?
    var state = "sending"   // sending | queued | accepted | executing | completed | failed | cancelled | unknown
    var reply: String?
    var error: String?
    var position: Int?
    var stepsDone: Int?
    var stepsTotal: Int?
    var simulated = false   // taken by the simulated dog, not the real one
    var lostContact = false // the dog stopped answering before this finished

    var finished: Bool { ["completed", "failed", "cancelled", "unknown"].contains(state) }
}

/// One of Annie's own exchanges with someone at home, placed on the timeline.
struct ConversationCard: Identifiable, Equatable {
    let id: String   // the dedupe key: which run of the dog process, and its t_s
    let at: Int      // wall-clock ms, worked out from the dog's clock when first seen
    let exchange: DogConversation
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
    @Published private(set) var reminderRunIDs: [Int: String] = [:]
    @Published private(set) var reminderInFlight: Int?
    @Published var reminderNote: CommandNote?
    @Published private(set) var pausing = false

    // Instructions that went straight to the dog, and Annie's own exchanges
    // with whoever she met at home. Both live on the Ask Annie timeline.
    @Published private(set) var instructions: [DirectInstruction] = []
    @Published private(set) var conversations: [ConversationCard] = []
    /// Runs that were meant for the dog directly but went as a family errand
    /// because she wasn't answering; the timeline says so under the message.
    @Published private(set) var fallbackRunIDs: Set<String> = []

    // People Annie knows (Profile). nil until the first answer.
    @Published private(set) var people: [KnownPerson]?
    @Published private(set) var peopleAvailable = false
    @Published var peopleNote: CommandNote?

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
    private var voiceRevision = UUID()

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

    // Conversations carry only the dog process' own clock (`t_s`, seconds
    // since it started). When that clock jumps backwards she was restarted, so
    // the same t_s can mean a different exchange: the run number keeps them apart.
    private var seenConversations: Set<String> = []
    private var dogRun = 0
    private var lastDogClock: Double?
    private var familyPollTask: Task<Void, Never>?

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
            startFamilyPolling()
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

    /// Other relatives and robot callbacks must update this phone even when
    /// its owner has not pressed Send. Fetch thread and run states together.
    private func startFamilyPolling() {
        familyPollTask?.cancel()
        familyPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshFamily()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func refreshFamily() async {
        guard live, let snapshot = try? await api.familySnapshot() else { return }
        thread = snapshot.thread
        for run in snapshot.runs { runs[run.run_id] = run }
        if let fresh = try? await api.reminders() { reminders = fresh }
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

    /// Whatever was typed, spoken, or tapped on a chip. An observation question
    /// is answered from memory. An explicit delivery ask (tell/remind/check on/
    /// ask someone) is a family message, because only a family run records the
    /// outcome — the resident's reply, a reminder done, an emergency. A motion
    /// or operator instruction goes straight to the dog's agent when she is
    /// answering.
    func submit(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let intent = MessageRouter.route(trimmed)
        if intent == .question {
            await ask(trimmed)
            return
        }
        if intent == .familyMessage {
            if live, dog == nil { await refreshDog() }   // asked before the first status came back
            await send(trimmed, insteadOfDog: live && dog?.available != true)
            return
        }
        if live, dog == nil { await refreshDog() }   // asked before the first status came back
        if dog?.available == true {
            await instruct(trimmed)
        } else {
            await send(trimmed, insteadOfDog: live)
        }
    }

    // MARK: Messages to Annie

    /// Hand the message to the backend and start watching the run it created.
    /// This returns as soon as the backend accepts it — the dog's errand takes
    /// a minute or more, and the UI must never sit blocked on it.
    ///
    /// `insteadOfDog` marks a message that would have gone to the dog directly
    /// had she been answering, so the timeline can say which path it took.
    func send(_ text: String, insteadOfDog: Bool = false) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !sending else { return }
        sending = true
        defer { sending = false }
        await deliverMessage(trimmed, insteadOfDog: insteadOfDog)
    }

    /// The family-errand path itself. Callers hold `sending`.
    @discardableResult
    private func deliverMessage(_ trimmed: String, insteadOfDog: Bool, reminderID: Int? = nil) async -> String? {
        sendError = nil
        guard live else {
            sendError = "Not connected to Annie. Set the server under Profile, Settings, Advanced."
            return nil
        }
        await refreshDog()
        guard let dog, dog.available, dog.connected else {
            sendError = "Your phone reached the server, but Annie is offline. The request wasn't queued."
            return nil
        }
        guard dog.motion_enabled != false else {
            sendError = "Annie is connected in camera-only mode. Enable movement on the robot service before sending an in-person request."
            return nil
        }
        do {
            let ack = try await api.sendMessage(authorID: authorID, text: trimmed, reminderID: reminderID)
            if insteadOfDog { fallbackRunIDs.insert(ack.run_id) }
            thread = (try? await api.thread()) ?? thread
            await refreshRuns()
            watchUnfinishedRuns()
            return ack.run_id
        } catch {
            sendError = humanMessage(for: error)
            // Only a request that never arrived means the backend is gone. A
            // "busy" or "not understood" answer is the backend working.
            if isTransportFailure(error) { live = false }
            return nil
        }
    }

    func reminderRun(for reminder: Reminder) -> FamilyRun? {
        if let latest = runs.values.filter({ $0.reminder_id == reminder.id }).max(by: { $0.created_at < $1.created_at }) {
            return latest
        }
        if let id = reminderRunIDs[reminder.id], let run = runs[id] { return run }
        return nil
    }

    func remind(_ reminder: Reminder) async {
        guard !sending, !pausing else { return }
        if let run = reminderRun(for: reminder), !run.finished { return }
        sending = true
        reminderInFlight = reminder.id
        reminderNote = nil
        defer { sending = false; reminderInFlight = nil }
        let text = "tell Grandma to \(reminder.title.prefix(1).lowercased() + reminder.title.dropFirst())"
        if let runID = await deliverMessage(text, insteadOfDog: false, reminderID: reminder.id) {
            reminderRunIDs[reminder.id] = runID
            reminderNote = .init(text: "Request received. Progress appears below the reminder and in Ask Annie.", isError: false)
        } else {
            reminderNote = .init(text: sendError ?? "The reminder wasn't sent.", isError: true)
        }
    }

    /// Pause cancels unfinished errands; a new explicit request starts fresh.
    /// A network acknowledgment alone never proves that the physical dog stopped.
    func pauseTasks() async {
        guard !pausing else { return }
        reminderNote = nil
        guard live else {
            commandNote = .init(text: "Can't reach the server to pause Annie. Check her directly.", isError: true)
            return
        }
        pausing = true
        defer { pausing = false }
        do {
            let receipt = try await api.pauseFamily()
            commandNote = .init(text: receipt.stop_confirmed
                ? "Paused. Unfinished tasks were cancelled. Send a new request when ready."
                : "Tasks cancelled, but Annie's stop wasn't confirmed. Check her directly.",
                isError: !receipt.stop_confirmed)
            await refreshRuns()
            await refreshDog()
        } catch {
            commandNote = .init(text: "Pause wasn't confirmed. " + humanMessage(for: error), isError: true)
        }
    }

    // MARK: Straight to the dog

    /// A plain-language instruction for the dog's situated agent. Returns once
    /// the mission board has taken it (or refused); what happens next arrives
    /// through the 3 s status poll and lands on the same timeline item.
    func instruct(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !sending else { return }
        sending = true
        sendError = nil
        defer { sending = false }

        let item = DirectInstruction(text: trimmed, at: Self.nowMs(), simulated: dog?.isSimulated ?? false)
        instructions.append(item)
        do {
            let receipt = try await api.dogInstruct(trimmed, author: authorID)
            updateInstruction(item.id) {
                $0.commandID = receipt.command_id
                $0.state = receipt.state ?? "accepted"
                $0.position = receipt.position
            }
            await refreshDog()
        } catch APIError.badStatus(let code) where [502, 503, 504].contains(code) {
            // The backend forwarded the instruction and the answer was lost —
            // for any of these codes, including 503 (the backend's own timeout
            // reads as 503), the dog may already have the ask in hand. It is
            // NEVER re-sent on another path: that could make Annie do the
            // thing twice. The family is told to check her before re-sending.
            dog = .offline
            updateInstruction(item.id) {
                $0.state = "unknown"
                $0.error = "Annie may have received this instruction. Check her status before sending it again."
            }
        } catch APIError.badStatus(409) {
            // The dog's mission board refused because it is full: the backend
            // delivered the ask, so it must not be sent again as an errand.
            updateInstruction(item.id) {
                $0.state = "failed"
                $0.error = humanMessage(for: APIError.badStatus(409))
            }
            await refreshDog()
        } catch APIError.badStatus(400) {
            updateInstruction(item.id) {
                $0.state = "failed"
                $0.error = humanMessage(for: APIError.badStatus(400))
            }
        } catch {
            updateInstruction(item.id) {
                $0.state = "failed"
                $0.error = humanMessage(for: error)
            }
            // A request lost mid-flight (URLError) is just as ambiguous: the
            // dog may still have it. Failed here, and re-sent only by a person
            // who has checked her status.
            if isTransportFailure(error) {
                live = false
                updateInstruction(item.id) {
                    $0.error = "Annie may have received this instruction. Check her status before sending it again."
                }
            }
        }
    }

    private func updateInstruction(_ id: UUID, _ change: (inout DirectInstruction) -> Void) {
        guard let index = instructions.firstIndex(where: { $0.id == id }) else { return }
        change(&instructions[index])
    }

    /// An unfinished instruction that has dropped off the dog's short mission
    /// list for this long is called unknown instead of spinning forever.
    private static let instructionWatchLimit = 300_000  // ms

    private static func nowMs() -> Int { Int(Date().timeIntervalSince1970 * 1000) }

    /// Fold one status answer into the timeline: new exchanges Annie had, and
    /// where each unfinished instruction has got to.
    private func ingest(_ status: DogStatus) {
        let now = Self.nowMs()

        for index in instructions.indices where !instructions[index].finished {
            guard status.available else {
                instructions[index].lostContact = true
                continue
            }
            instructions[index].lostContact = false
            guard let commandID = instructions[index].commandID else { continue }
            if let mission = status.missions.first(where: { $0.command_id == commandID }) {
                if !mission.state.isEmpty { instructions[index].state = mission.state }
                instructions[index].reply = mission.reply ?? instructions[index].reply
                instructions[index].error = mission.error
                instructions[index].position = mission.position
                instructions[index].stepsDone = mission.stepsDone
                instructions[index].stepsTotal = mission.stepsTotal
            } else if now - instructions[index].at > Self.instructionWatchLimit {
                instructions[index].state = "unknown"
            }
        }

        guard status.available else { return }
        if let clock = status.t_s {
            if let last = lastDogClock, clock + 5 < last { dogRun += 1 }   // her clock started again
            lastDogClock = clock
        }
        for exchange in status.conversations {
            let key = "\(dogRun)-\(exchange.t_s)"
            guard seenConversations.insert(key).inserted else { continue }
            let ageSeconds = max(0, (status.t_s ?? exchange.t_s) - exchange.t_s)
            conversations.append(ConversationCard(id: key, at: now - Int(ageSeconds * 1000), exchange: exchange))
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
        let status = (try? await api.dogStatus()) ?? .offline
        dog = status
        ingest(status)
    }

    /// A Controls button. Stop is never blocked behind another command.
    func command(_ action: DogAction) async {
        if action == .stop { await pauseTasks(); return }
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

    // MARK: People Annie knows

    func loadPeople() async {
        guard live else {
            people = nil
            peopleAvailable = false
            return
        }
        guard let answer = try? await api.people() else {
            peopleAvailable = false
            people = people ?? []
            return
        }
        peopleAvailable = answer.available
        // An unavailable dog answers with an empty list; keep showing the last
        // real one rather than implying everyone was forgotten.
        if answer.available || people == nil { people = answer.people }
    }

    /// Returns nil when the person was enrolled, or the words to show when not.
    func addPerson(_ person: NewPerson) async -> String? {
        guard live else { return "Not connected to Annie. Set the server under Settings, Advanced." }
        do {
            let result = try await api.addPerson(person)
            let name = result.name ?? person.name
            if person.photos.isEmpty {
                peopleNote = CommandNote(text: "Annie knows \(name) by name. Add photos so she can recognise them.", isError: false)
            } else if let added = result.faces_added, added > 0 {
                peopleNote = CommandNote(text: "Annie found \(name)'s face in \(added) of \(person.photos.count) photo\(person.photos.count == 1 ? "" : "s").", isError: false)
            } else {
                // Saved, but no usable face: say so instead of implying she can recognise them.
                peopleNote = CommandNote(text: "\(name) is saved, but Annie couldn't find a face in the photos. Try a clear, front-facing one.", isError: true)
            }
            await loadPeople()
            return nil
        } catch APIError.badStatus(let code) where code == 400 || code == 422 {
            return "Annie couldn't use that name. Use letters, spaces, apostrophes or hyphens, up to 40."
        } catch {
            if isTransportFailure(error) { live = false }
            return humanMessage(for: error)
        }
    }

    func forget(_ person: KnownPerson) async {
        guard live else { return }
        do {
            try await api.forgetPerson(named: person.name)
            peopleNote = CommandNote(text: "Annie has forgotten \(person.name).", isError: false)
        } catch APIError.badStatus(404) {
            peopleNote = nil   // already gone; the refreshed list says the same
        } catch {
            peopleNote = CommandNote(text: humanMessage(for: error), isError: true)
            if isTransportFailure(error) { live = false }
        }
        await loadPeople()
    }

    // MARK: Voice settings

    func loadVoiceSettings() async {
        guard !voiceSaving else { return }
        guard live else {
            voice = nil
            return
        }
        let revision = voiceRevision
        let loaded = (try? await api.voiceSettings()) ?? .unavailable
        guard !voiceSaving, revision == voiceRevision else { return }
        voice = loaded
        if voice?.muted == true { synthesizer.stopSpeaking(at: .immediate) }
    }

    /// Send only what changed. Returns true when the dog took the change, so a
    /// caller holding a just-typed key knows whether it was delivered.
    @discardableResult
    func updateVoice(_ update: VoiceSettingsUpdate, saved message: String) async -> Bool {
        guard live else {
            voiceNote = CommandNote(text: "Not connected to Annie. Set the server under Advanced.", isError: true)
            return false
        }
        guard !voiceSaving else { return false }
        voiceSaving = true
        voiceRevision = UUID()
        defer { voiceSaving = false }
        do {
            voice = try await api.updateVoiceSettings(update)
            if voice?.muted == true { synthesizer.stopSpeaking(at: .immediate) }
            if let expected = update.muted, voice?.muted != expected {
                voiceNote = CommandNote(text: "Audio change was not confirmed. Please retry.", isError: true)
                return false
            }
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
        guard voice?.muted != true else { return }
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
