import XCTest
@testable import AnnieApp

actor FakeAPI: AnnieServing {
    var failingMessages = false
    var failingReminders = false
    var keys: [String] = []
    var holdUser: Int?
    var held: CheckedContinuation<ReminderPage, Error>?
    let dog = DogUser(id: 1, name: "Jeanine", timezone: "America/New_York")

    func users() async throws -> [AppUser] { [AppUser(id: 2, name: "Zach", dog_user_id: 1), AppUser(id: 3, name: "Ellis", dog_user_id: 1)] }
    func residents() async throws -> [DogUser] { [dog] }
    func reminders(userID: Int) async throws -> ReminderPage {
        if userID == holdUser {
            return try await withCheckedThrowingContinuation { held = $0 }
        }
        return ReminderPage(items: [reminder(id: userID)], next_cursor: nil, day: residentDay(timezone: dog.timezone))
    }
    func conversation(userID: Int) async throws -> ConversationPage {
        ConversationPage(id: userID, app_user_id: userID, dog_user_id: 1, day: residentDay(timezone: dog.timezone),
                         messages: [MessageEntry(id: userID, role: "app_user", text: "User \(userID)", timestamp: "2026-09-20T12:00:00Z", request_id: userID + 10, reply_to: nil, status: "queued")], next_cursor: nil)
    }
    func history(userID: Int, cursor: String?) async throws -> Page<HistoryItem> { Page(items: [], next_cursor: nil) }
    func addReminder(_ body: NewReminder, key: String) async throws -> Reminder {
        if failingReminders { throw URLError(.timedOut) }
        return reminder(id: 20)
    }
    func sendMessage(_ body: NewMessage, key: String) async throws -> DispatchReceipt {
        keys.append(key)
        if failingMessages { throw URLError(.timedOut) }
        return DispatchReceipt(conversation_id: 100, message_id: 101, request_id: 102, day: "2026-09-20", status: "queued")
    }
    func reminder(id: Int) -> Reminder {
        Reminder(id: id, dog_user_id: 1, daily_time: "09:30", timestamp: "2026-09-20T12:00:00Z", description: "Reminder \(id)", enabled: true, done: false, latest_note: nil)
    }
    func failMessages(_ value: Bool) { failingMessages = value }
    func failReminders(_ value: Bool) { failingReminders = value }
    func hold(_ user: Int) { holdUser = user }
    func waitForHeldRequest() async {
        while held == nil { await Task.yield() }
    }
    func releaseHeld() {
        held?.resume(returning: ReminderPage(items: [reminder(id: 999)], next_cursor: nil, day: "2026-09-20"))
        held = nil
    }
}

@MainActor
final class StateTests: XCTestCase {
    func profile(_ id: Int) -> Profile {
        Profile(user: AppUser(id: id, name: "User \(id)", dog_user_id: 1),
                resident: DogUser(id: 1, name: "Jeanine", timezone: "America/New_York"), server: AppConfiguration.apiBaseURL.absoluteString)
    }

    func testSwitchingAccountsRejectsLateResponses() async {
        let api = FakeAPI()
        let state = AppState(api: api, useSocket: false)
        await api.hold(2)
        let previous = Task { await state.activate(profile(2)) }
        await api.waitForHeldRequest()
        await state.activate(profile(3))
        await api.releaseHeld()
        await previous.value
        XCTAssertEqual(state.profile?.user.id, 3)
        XCTAssertEqual(state.reminders.map(\.id), [3])
        XCTAssertEqual(state.messages.map(\.text), ["User 3"])
        state.deactivate()
        XCTAssertTrue(state.messages.isEmpty)
    }

    func testRetryKeepsKeyAcrossStateRecreation() async {
        let suite = "AnnieTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let api = FakeAPI()
        var state = AppState(api: api, useSocket: false, defaults: defaults)
        await state.activate(profile(2))
        await api.failMessages(true)
        let failed = await state.send("Hello")
        XCTAssertFalse(failed)
        state.deactivate()
        state = AppState(api: api, useSocket: false, defaults: defaults)
        await state.activate(profile(2))
        await api.failMessages(false)
        let success = await state.send("Hello")
        XCTAssertTrue(success)
        let keys = await api.keys
        XCTAssertEqual(keys.count, 2)
        XCTAssertEqual(keys[0], keys[1])
        let next = await state.send("Hello")
        XCTAssertTrue(next)
        let nextKeys = await api.keys
        XCTAssertNotEqual(nextKeys[1], nextKeys[2])
        state.deactivate()
    }

    func testFailedReminderIsNotAddedLocally() async {
        let api = FakeAPI()
        let state = AppState(api: api, useSocket: false)
        await state.activate(profile(2))
        let previous = state.reminders
        await api.failReminders(true)
        let result = await state.add(time: "14:30", description: "Test")
        XCTAssertFalse(result)
        XCTAssertEqual(state.reminders, previous)
        XCTAssertNotNil(state.addError)
        state.deactivate()
    }

    func testLegacyProfileRequiresBackendSelection() {
        let suite = "AnnieProfiles-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set(Data("{\"member\":\"zach\"}".utf8), forKey: "annieProfile")
        XCTAssertNil(ProfileStore(defaults: defaults).load())
        ProfileStore(defaults: defaults).save(profile(2))
        XCTAssertEqual(ProfileStore(defaults: defaults).load()?.user.id, 2)
    }

    func testResidentDayUsesTimezoneAndParsesFractionalTimestamp() {
        let instant = parseTimestamp("2026-09-21T02:30:00.123Z")!
        XCTAssertEqual(residentDay(timezone: "America/New_York", now: instant), "2026-09-20")
        XCTAssertNotNil(parseTimestamp("2026-09-21T02:30:00Z"))
    }
}
