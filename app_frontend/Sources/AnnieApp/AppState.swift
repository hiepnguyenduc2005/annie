import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published private(set) var profile: Profile?
    @Published private(set) var reminders: [Reminder] = []
    @Published private(set) var messages: [MessageEntry] = []
    @Published private(set) var history: [HistoryItem] = []
    @Published private(set) var conversationDay = ""
    @Published private(set) var historyCursor: String?
    @Published private(set) var live = false
    @Published private(set) var socketConnected = false
    @Published private(set) var loading = false
    @Published private(set) var sending = false
    @Published private(set) var adding = false
    @Published private(set) var loadingHistory = false
    @Published private(set) var reminderError: String?
    @Published private(set) var messageError: String?
    @Published private(set) var historyError: String?
    @Published private(set) var sendError: String?
    @Published private(set) var addError: String?
    let serverURL = AppConfiguration.apiBaseURL

    var timezone: String { profile?.resident.timezone ?? "America/New_York" }
    var residentName: String { profile?.resident.name ?? "your resident" }
    var connectionError: String? { reminderError ?? messageError ?? historyError }
    private let api: any AnnieServing
    private let socket = LiveConnection()
    private let useSocket: Bool
    private let defaults: UserDefaults
    private var generation = UUID()
    private var reminderRevision = UUID()
    private var messageRevision = UUID()
    private var historyRevision = UUID()
    private var refreshTask: Task<Void, Never>?

    init(api: any AnnieServing = AnnieAPI(), useSocket: Bool = true, defaults: UserDefaults = .standard) {
        self.api = api
        self.useSocket = useSocket
        self.defaults = defaults
    }

    func activate(_ profile: Profile) async {
        deactivate()
        self.profile = profile
        let current = generation
        await load()
        guard generation == current else { return }
        startLiveUpdates()
        startPeriodicRefresh()
    }

    private func startPeriodicRefresh() {
        refreshTask?.cancel()
        let current = generation
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard !Task.isCancelled, let self, self.generation == current else { return }
                // Refetch today at resident midnight even if no WebSocket event occurs.
                if self.conversationDay != residentDay(timezone: self.timezone) || !self.socketConnected {
                    await self.load()
                } else {
                    // Time-based completion can change without a new report event.
                    await self.refreshReminders()
                }
            }
        }
    }

    func deactivate() {
        generation = UUID()
        socket.stop()
        refreshTask?.cancel()
        refreshTask = nil
        profile = nil
        reminders = []; messages = []; history = []
        historyCursor = nil; conversationDay = ""
        live = false; socketConnected = false; loading = false
        sending = false; adding = false; loadingHistory = false
        reminderError = nil; messageError = nil; historyError = nil; sendError = nil; addError = nil
    }

    func load() async {
        guard profile != nil else { return }
        let current = generation
        loading = true
        async let a: Void = refreshReminders()
        async let b: Void = refreshConversation()
        async let c: Void = refreshHistory()
        _ = await (a, b, c)
        guard current == generation else { return }
        loading = false
    }

    func foreground() async {
        guard profile != nil else { return }
        let current = generation
        await load()
        guard current == generation else { return }
        startLiveUpdates()
        startPeriodicRefresh()
    }

    func suspend() {
        socket.stop()
        socketConnected = false
        refreshTask?.cancel()
        refreshTask = nil
    }

    private func startLiveUpdates() {
        guard useSocket, let profile else { return }
        let current = generation
        socket.start(userID: profile.user.id, onEvent: { [weak self] type in
            guard let self, self.generation == current else { return }
            switch type {
            case "connected", "resync_required": await self.load()
            case "reminder.created": await self.refreshReminders()
            case "note.created":
                async let a: Void = self.refreshReminders()
                async let b: Void = self.refreshHistory()
                _ = await (a, b)
            case "notification.created": await self.refreshHistory()
            case "message.created", "request.updated": await self.refreshConversation()
            default: break
            }
        }, onStatus: { [weak self] connected in
            guard let self, self.generation == current else { return }
            self.socketConnected = connected
        })
    }

    func refreshReminders() async {
        guard let profile else { return }
        let current = generation, revision = UUID()
        reminderRevision = revision
        do {
            let page = try await api.reminders(userID: profile.user.id)
            guard current == generation, revision == reminderRevision else { return }
            reminders = page.items
            reminderError = nil
            live = true
        } catch {
            guard current == generation, revision == reminderRevision else { return }
            reminderError = connectionMessage(error)
            if error is URLError { live = false }
        }
    }

    func refreshConversation() async {
        guard let profile else { return }
        let current = generation, revision = UUID()
        messageRevision = revision
        let today = residentDay(timezone: timezone)
        if !conversationDay.isEmpty && conversationDay != today {
            messages = []
            conversationDay = today
        }
        do {
            let page = try await api.conversation(userID: profile.user.id)
            guard current == generation, revision == messageRevision else { return }
            guard page.app_user_id == profile.user.id, page.dog_user_id == profile.user.dog_user_id else { throw APIError.invalidResponse }
            messages = page.messages
            conversationDay = page.day
            messageError = nil
            live = true
        } catch {
            guard current == generation, revision == messageRevision else { return }
            messageError = connectionMessage(error)
            if error is URLError { live = false }
        }
    }

    func refreshHistory(more: Bool = false) async {
        guard let profile else { return }
        if more && (loadingHistory || historyCursor == nil) { return }
        let current = generation, revision = UUID()
        historyRevision = revision
        loadingHistory = true
        let cursor = more ? historyCursor : nil
        do {
            let page = try await api.history(userID: profile.user.id, cursor: cursor)
            guard current == generation, revision == historyRevision else { return }
            if more {
                let existing = Set(history.map(\.id))
                history += page.items.filter { !existing.contains($0.id) }
            } else { history = page.items }
            historyCursor = page.next_cursor
            historyError = nil
            live = true
        } catch {
            guard current == generation, revision == historyRevision else { return }
            historyError = connectionMessage(error)
            if error is URLError { live = false }
        }
        if current == generation, revision == historyRevision { loadingHistory = false }
    }

    // Retain the key across timeout/relaunch. Never reuse it for another account or payload.
    private func retryKey<Body: Encodable>(_ action: String, body: Body) -> (String, String) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        let payload = (try? encoder.encode(body)) ?? Data()
        let storage = "annie.retry.\(serverURL.absoluteString).\(profile!.user.id).\(action)"
        if let old = defaults.dictionary(forKey: storage), let oldBody = old["body"] as? Data,
           oldBody == payload, let key = old["key"] as? String { return (storage, key) }
        let key = UUID().uuidString
        defaults.set(["body": payload, "key": key], forKey: storage)
        return (storage, key)
    }

    func send(_ text: String) async -> Bool {
        guard let profile, !sending else { return false }
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 4000 else { return false }
        let current = generation
        sending = true; sendError = nil
        let body = NewMessage(app_user_id: profile.user.id, text: text)
        let (storage, key) = retryKey("message", body: body)
        defer { if current == generation { sending = false } }
        do {
            _ = try await api.sendMessage(body, key: key)
            if defaults.dictionary(forKey: storage)?["key"] as? String == key { defaults.removeObject(forKey: storage) }
            guard current == generation else { return false }
            await refreshConversation()
            return true
        } catch {
            guard current == generation else { return false }
            sendError = connectionMessage(error) + " You can retry this message."
            return false
        }
    }

    func add(time: String, description: String) async -> Bool {
        guard let profile, !adding else { return false }
        let text = description.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 4000 else { return false }
        let current = generation
        adding = true; addError = nil
        let body = NewReminder(app_user_id: profile.user.id, daily_time: time, description: text)
        let (storage, key) = retryKey("reminder", body: body)
        defer { if current == generation { adding = false } }
        do {
            _ = try await api.addReminder(body, key: key)
            if defaults.dictionary(forKey: storage)?["key"] as? String == key { defaults.removeObject(forKey: storage) }
            guard current == generation else { return false }
            await refreshReminders()
            return true
        } catch {
            guard current == generation else { return false }
            addError = connectionMessage(error)
            return false
        }
    }
}
