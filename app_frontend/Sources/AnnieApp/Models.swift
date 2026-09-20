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

    /// The backend gives facts merged from the dog's live memory negative
    /// ids, so they can never collide with stored ones.
    var isLive: Bool { id < 0 }
}

struct AskRequest: Codable {
    let question: String
}

struct AskResponse: Codable {
    let answer: String
}

// ---------------------------------------------------------------------------
// Family view: messages Zach sends through Annie, and how each one goes
// ---------------------------------------------------------------------------

struct ThreadMessage: Codable, Identifiable, Equatable {
    let message_id: String
    let author_id: String
    let text: String
    let at: Int
    let run_id: String

    var id: String { message_id }
}

struct NewMessage: Codable {
    let author_id: String
    let text: String
    var reminder_id: Int? = nil
}

struct FamilyPauseReceipt: Decodable {
    let paused: Bool
    let cancelled_runs: [String]
    let stop_confirmed: Bool
}

struct FamilySnapshot: Decodable {
    let thread: [ThreadMessage]
    let runs: [FamilyRun]
}

struct DispatchAck: Codable {
    let run_id: String
    let status: String
}

/// One beat of a run. `summary` and `speaker` are rendered by the backend so
/// this client never has to interpret a robot-supplied payload.
struct RunEvent: Codable, Identifiable, Equatable {
    let event_id: String
    let kind: String
    let at: Int
    let summary: String
    let speaker: String

    var id: String { event_id }
    var isAnnie: Bool { speaker == "annie" }
    var isResident: Bool { speaker == "resident" }
}

struct FamilyRun: Codable, Identifiable, Equatable {
    let run_id: String
    let author_id: String
    let text: String
    let status: String
    let created_at: Int
    let events: [RunEvent]
    var reminder_id: Int? = nil

    var id: String { run_id }

    /// True once the robot can send nothing further for this run.
    var finished: Bool { ["completed", "failed", "unreachable", "cancelled", "paused", "unknown"].contains(status) }

    var statusLabel: String {
        switch status {
        case "dispatched", "accepted", "queued": return "Queued · waiting for Annie"
        case "running":
            switch events.last?.kind {
            case "navigating": return "Looking for Jeanine"
            case "arrived": return "Found Jeanine"
            case "speaking": return "Speaking to Jeanine"
            case "listening": return "Listening for her reply"
            case "heard": return "Reply received"
            case "recalling", "recalled": return "Checking memory"
            default: return "Waiting for execution confirmation"
            }
        case "completed": return "Delivered"
        case "failed": return "Annie couldn't finish"
        case "unreachable": return "Couldn't reach Annie"
        case "cancelled", "paused": return "Paused · task cancelled"
        case "unknown": return "Status unavailable · check Annie"
        default: return status
        }
    }
}

// ---------------------------------------------------------------------------
// The dog: live status, direct controls, and how Annie speaks and hears
//
// These payloads are forwarded from the dog process, which runs a different
// build from the backend at times, so every field decodes leniently: a
// missing or oddly typed field becomes nil instead of failing the whole read.
// ---------------------------------------------------------------------------

/// One mission receipt from the dog's mission board. A typed instruction is
/// an `instruct` mission: the situated agent plans it into steps, and its
/// spoken reply rides along in `progress.reply`, then `result.reply`.
struct DogMission: Decodable, Identifiable, Equatable {
    let command_id: String
    let name: String
    let state: String     // queued | accepted | executing | completed | failed | cancelled
    let detail: String?   // args.text / args.thing, when the mission has one
    let reply: String?    // what Annie said she would do, once the plan exists
    let error: String?
    let position: Int?    // place in the queue while state == "queued"
    let stepsDone: Int?
    let stepsTotal: Int?

    var id: String { command_id }

    private enum Keys: String, CodingKey { case command_id, name, state, args, result, error, progress, position }
    private enum ArgKeys: String, CodingKey { case text, thing, person }
    private enum ResultKeys: String, CodingKey { case reply }
    private enum ProgressKeys: String, CodingKey { case reply, steps, done }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = (try? c.decode(String.self, forKey: .name)) ?? "mission"
        state = (try? c.decode(String.self, forKey: .state)) ?? ""
        command_id = (try? c.decode(String.self, forKey: .command_id)) ?? UUID().uuidString
        let args = try? c.nestedContainer(keyedBy: ArgKeys.self, forKey: .args)
        detail = (try? args?.decode(String.self, forKey: .text))
            ?? (try? args?.decode(String.self, forKey: .thing))
            ?? (try? args?.decode(String.self, forKey: .person))
        // `result` is a dictionary for an instruction and anything at all for
        // other missions, so it is only read when it has the expected shape.
        let result = try? c.nestedContainer(keyedBy: ResultKeys.self, forKey: .result)
        let progress = try? c.nestedContainer(keyedBy: ProgressKeys.self, forKey: .progress)
        let spoken = (try? result?.decode(String.self, forKey: .reply)) ?? (try? progress?.decode(String.self, forKey: .reply))
        reply = spoken.flatMap { $0.isEmpty ? nil : $0 }
        error = (try? c.decode(String.self, forKey: .error)).flatMap { $0.isEmpty ? nil : $0 }
        position = try? c.decode(Int.self, forKey: .position)
        stepsDone = try? progress?.decode(Int.self, forKey: .done)
        stepsTotal = try? progress?.decode(Int.self, forKey: .steps)
    }

    var isActive: Bool { state == "accepted" || state == "executing" }
}

/// One exchange between Annie and someone at home, as the dog reports it.
/// `t_s` is seconds since the dog process started, and is the only identity
/// an exchange has.
struct DogConversation: Decodable, Equatable {
    let t_s: Double
    let name: String?    // who she recognised, when she did
    let asked: String?   // what Annie said first (nil when the person spoke first)
    let heard: String?   // what the microphone made of the answer
    let reply: String?   // what Annie said back
    let kind: String     // "concern" when what was heard worried her

    private enum Keys: String, CodingKey { case t_s, name, asked, heard, reply, kind }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        t_s = try c.decode(Double.self, forKey: .t_s)   // no timestamp, no identity: the exchange is skipped
        func text(_ key: Keys) -> String? {
            let value = (try? c.decode(String.self, forKey: key))?.trimmingCharacters(in: .whitespacesAndNewlines)
            return (value ?? "").isEmpty ? nil : value
        }
        name = text(.name); asked = text(.asked); heard = text(.heard); reply = text(.reply)
        kind = text(.kind) ?? ""
    }

    var isConcern: Bool { kind.lowercased() == "concern" }
}

/// Decodes what it can of an array and drops elements that don't fit, so one
/// malformed entry from the dog never blanks the rest.
struct LossyArray<Element: Decodable>: Decodable {
    let elements: [Element]

    private struct Skipped: Decodable { init(from decoder: Decoder) throws {} }

    init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        var kept: [Element] = []
        while !container.isAtEnd {
            if let element = try? container.decode(Element.self) {
                kept.append(element)
            } else if (try? container.decode(Skipped.self)) == nil {
                break   // can't step past it: stop rather than spin
            }
        }
        elements = kept
    }
}

/// `GET /api/dog/status`. `available == false` means the backend could not
/// reach the dog process at all.
struct DogStatus: Decodable, Equatable {
    let available: Bool
    let connected: Bool
    let mode: String?
    let action: String?
    let battery: Double?      // percent, 0-100
    let people: Int?
    let greetings: Int?
    let checkins: Int?
    let missions: [DogMission]
    let sentences: [String]   // what Annie remembers, already in plain words
    let source: String?       // "hardware" or "simulation"
    var motion_enabled: Bool? = nil
    var paused: Bool? = nil
    let t_s: Double?          // the dog process' clock now, to date `conversations`
    let conversations: [DogConversation]

    static let offline = DogStatus(available: false, connected: false)

    private enum Keys: String, CodingKey {
        case available, connected, mode, action, battery, people, greetings, checkins, missions, sentences
        case source, t_s, conversations, motion_enabled, paused
    }

    init(available: Bool, connected: Bool, mode: String? = nil, action: String? = nil, battery: Double? = nil,
         people: Int? = nil, greetings: Int? = nil, checkins: Int? = nil, missions: [DogMission] = [],
         sentences: [String] = [], source: String? = nil, t_s: Double? = nil, conversations: [DogConversation] = []) {
        self.available = available; self.connected = connected; self.mode = mode; self.action = action
        self.battery = battery; self.people = people; self.greetings = greetings; self.checkins = checkins
        self.missions = missions; self.sentences = sentences
        self.source = source; self.t_s = t_s; self.conversations = conversations
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        available = (try? c.decode(Bool.self, forKey: .available)) ?? false
        connected = (try? c.decode(Bool.self, forKey: .connected)) ?? false
        mode = try? c.decode(String.self, forKey: .mode)
        action = try? c.decode(String.self, forKey: .action)
        battery = try? c.decode(Double.self, forKey: .battery)
        people = try? c.decode(Int.self, forKey: .people)
        greetings = try? c.decode(Int.self, forKey: .greetings)
        checkins = try? c.decode(Int.self, forKey: .checkins)
        missions = (try? c.decode([DogMission].self, forKey: .missions)) ?? []
        sentences = (try? c.decode([String].self, forKey: .sentences)) ?? []
        source = try? c.decode(String.self, forKey: .source)
        motion_enabled = try? c.decode(Bool.self, forKey: .motion_enabled)
        paused = try? c.decode(Bool.self, forKey: .paused)
        t_s = try? c.decode(Double.self, forKey: .t_s)
        conversations = (try? c.decode(LossyArray<DogConversation>.self, forKey: .conversations))?.elements ?? []
    }

    /// True when the dog answering is the simulated one. The family must never
    /// mistake a simulated run for the real dog moving in the real home.
    var isSimulated: Bool { available && source?.lowercased() == "simulation" }

    /// The patrol loop's mode in plain words. It reports "cruise", "follow",
    /// "voice_stop", planner states such as "blocked", and "brain:<action>"
    /// when the situated agent is steering; the prefix is dropped.
    var modeLabel: String {
        if motion_enabled == false { return "Camera only · movement disabled" }
        if paused == true { return "Paused · ready for a new request" }
        let raw = (mode ?? "").lowercased()
        let key = raw.split(separator: ":").last.map(String.init) ?? raw
        switch key {
        case "", "-", "idle", "stop", "stopped": return "Standing by"
        case "cruise", "explore", "exploring", "wander": return "Exploring"
        case "follow": return "Following someone"
        case "approach": return "Walking up to someone"
        case "blocked": return "Stopped by an obstacle"
        case "backoff", "back_off": return "Backing away"
        case "turn", "turn_left", "turn_right": return "Turning"
        case "scan": return "Looking around"
        case "wait": return "Waiting"
        case "go_home", "home": return "Heading home"
        case "voice_stop": return "Stopped by voice"
        case "greet", "greeting", "hello": return "Saying hello"
        default: return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// Sentences worth showing a family member: what and who she last saw.
    /// The map-size summary ("... occupied voxels remembered") is for
    /// engineers, so it is only used when there is nothing else.
    var familySentences: [String] {
        let plain = sentences.filter { !$0.localizedCaseInsensitiveContains("voxel") }
        return plain.isEmpty ? sentences : plain
    }
}

/// The buttons on the Controls card. Raw values are the backend's action names.
enum DogAction: String, CaseIterable, Identifiable {
    case explore, scan, go_home, hello, dance, stop

    var id: String { rawValue }

    var label: String {
        switch self {
        case .explore: return "Explore"
        case .scan: return "Look around"
        case .go_home: return "Come home"
        case .hello: return "Wave"
        case .dance: return "Dance"
        case .stop: return "Stop"
        }
    }

    var symbol: String {
        switch self {
        case .explore: return "map"
        case .scan: return "binoculars"
        case .go_home: return "house"
        case .hello: return "hand.wave"
        case .dance: return "music.note"
        case .stop: return "stop.fill"
        }
    }

    /// What the card says once the dog has taken the command.
    var acknowledgement: String {
        switch self {
        case .explore: return "Annie is off exploring."
        case .scan: return "Annie is having a look around."
        case .go_home: return "Annie is heading home."
        case .hello: return "Annie is waving hello."
        case .dance: return "Annie is dancing."
        case .stop: return "Annie has been told to stop."
        }
    }
}

struct DogCommandBody: Encodable {
    var action: String?
    var text: String?
    var author: String?
}

/// What `POST /api/dog/command {text}` answers: the mission board's receipt
/// for the instruction. Only the id and first state matter here; everything
/// after that is read from `DogStatus.missions`.
struct InstructReceipt: Decodable {
    let command_id: String?
    let state: String?
    let position: Int?

    private enum Keys: String, CodingKey { case command_id, state, position }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        command_id = try? c.decode(String.self, forKey: .command_id)
        state = try? c.decode(String.self, forKey: .state)
        position = try? c.decode(Int.self, forKey: .position)
    }
}

// ---------------------------------------------------------------------------
// People Annie knows (GET/POST/DELETE /api/people)
//
// Photos are sent once for the face embedding and are not kept by the backend
// or the dog; this app does not keep them either.
// ---------------------------------------------------------------------------

struct KnownPerson: Decodable, Identifiable, Equatable {
    let name: String
    let relation: String?
    let shirt: String?
    let notes: String?
    let faces: Int        // how many face samples Annie holds for them
    let guest: Bool       // a visitor rather than a member of the household
    let added_at: Double?

    var id: String { name }

    private enum Keys: String, CodingKey { case name, relation, shirt, notes, faces, guest, added_at }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = try c.decode(String.self, forKey: .name)
        func text(_ key: Keys) -> String? {
            (try? c.decode(String.self, forKey: key)).flatMap { $0.isEmpty ? nil : $0 }
        }
        relation = text(.relation); shirt = text(.shirt)?.lowercased(); notes = text(.notes)
        faces = (try? c.decode(Int.self, forKey: .faces)) ?? 0
        guest = (try? c.decode(Bool.self, forKey: .guest)) ?? false
        added_at = try? c.decode(Double.self, forKey: .added_at)
    }
}

struct PeopleResponse: Decodable {
    let available: Bool
    let people: [KnownPerson]

    private enum Keys: String, CodingKey { case available, people }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        available = (try? c.decode(Bool.self, forKey: .available)) ?? false
        people = (try? c.decode(LossyArray<KnownPerson>.self, forKey: .people))?.elements ?? []
    }
}

/// nil fields are omitted from the JSON. `photos` are base64 JPEGs.
struct NewPerson: Encodable {
    let name: String
    var relation: String?
    var shirt: String?
    var notes: String?
    var photos: [String]
}

/// What enrolling answers: whether the photos actually gave Annie a face.
struct EnrolResult: Decodable {
    let name: String?
    let faces_added: Int?
    let faces_total: Int?
    let faces_error: String?

    private enum Keys: String, CodingKey { case name, faces_added, faces_total, faces_error }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = try? c.decode(String.self, forKey: .name)
        faces_added = try? c.decode(Int.self, forKey: .faces_added)
        faces_total = try? c.decode(Int.self, forKey: .faces_total)
        faces_error = (try? c.decode(String.self, forKey: .faces_error)).flatMap { $0.isEmpty ? nil : $0 }
    }
}

/// A microphone or speaker on the machine that runs the dog.
struct AudioDevice: Decodable, Identifiable, Equatable, Hashable {
    let index: Int
    let name: String
    let input: Bool
    let output: Bool

    var id: Int { index }
}

/// `GET/POST /api/settings/voice`. API keys are write-only: the backend only
/// ever reports whether one is set (`elevenlabs`, `deepgram`).
struct VoiceSettings: Decodable, Equatable {
    let muted: Bool?
    let available: Bool
    let cloud: Bool
    let elevenlabs: Bool
    let deepgram: Bool
    let speak_via: String
    let hear_via: String
    let devices: [AudioDevice]
    let input: String?
    let output: String?

    static let unavailable = VoiceSettings(available: false, cloud: false, elevenlabs: false, deepgram: false,
                                           speak_via: "off", hear_via: "off", devices: [], input: nil, output: nil)

    private enum Keys: String, CodingKey {
        case muted, available, cloud, elevenlabs, deepgram, speak_via, hear_via, devices, input, output
    }
    private enum DeviceKeys: String, CodingKey { case devices, input, output }

    init(available: Bool, cloud: Bool, elevenlabs: Bool, deepgram: Bool, speak_via: String, hear_via: String,
         devices: [AudioDevice], input: String?, output: String?, muted: Bool? = nil) {
        self.muted = muted
        self.available = available; self.cloud = cloud; self.elevenlabs = elevenlabs; self.deepgram = deepgram
        self.speak_via = speak_via; self.hear_via = hear_via; self.devices = devices
        self.input = input; self.output = output
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        muted = try c.decodeIfPresent(Bool.self, forKey: .muted)
        available = (try? c.decode(Bool.self, forKey: .available)) ?? false
        cloud = (try? c.decode(Bool.self, forKey: .cloud)) ?? false
        elevenlabs = (try? c.decode(Bool.self, forKey: .elevenlabs)) ?? false
        deepgram = (try? c.decode(Bool.self, forKey: .deepgram)) ?? false
        speak_via = (try? c.decode(String.self, forKey: .speak_via)) ?? "off"
        hear_via = (try? c.decode(String.self, forKey: .hear_via)) ?? "off"

        // The dog process nests the device list: devices = {input, output,
        // devices: [...]}. A flat array plus top-level input/output is
        // accepted too, so either producer shape works.
        if let flat = try? c.decode([AudioDevice].self, forKey: .devices) {
            devices = flat
            input = try? c.decode(String.self, forKey: .input)
            output = try? c.decode(String.self, forKey: .output)
        } else if let nested = try? c.nestedContainer(keyedBy: DeviceKeys.self, forKey: .devices) {
            devices = (try? nested.decode([AudioDevice].self, forKey: .devices)) ?? []
            input = (try? nested.decode(String.self, forKey: .input)) ?? (try? c.decode(String.self, forKey: .input))
            output = (try? nested.decode(String.self, forKey: .output)) ?? (try? c.decode(String.self, forKey: .output))
        } else {
            devices = []
            input = try? c.decode(String.self, forKey: .input)
            output = try? c.decode(String.self, forKey: .output)
        }
    }

    var microphones: [AudioDevice] { devices.filter(\.input) }
    var speakers: [AudioDevice] { devices.filter(\.output) }
}

/// Only the fields being changed are sent; nil fields are omitted from the JSON.
struct VoiceSettingsUpdate: Encodable {
    var muted: Bool?
    var cloud: Bool?
    var eleven_key: String?
    var deepgram_key: String?
    var input_device: String?
    var output_device: String?
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

/// "2026-09-19T15:20:00" -> "Yesterday 3:20 PM". History spans days, so a bare
/// clock time would make yesterday's fact read like today's. The backend's
/// timestamps carry no zone and are the home's local time; they are compared
/// by calendar day only, never converted.
func fmtDayStamp(_ iso: String, now: Date = Date(), calendar: Calendar = .current) -> String {
    let clock = fmtStamp(iso)
    let parts = iso.prefix(10).split(separator: "-").compactMap { Int($0) }
    guard parts.count == 3,
          let day = calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2])) else {
        return clock
    }
    let today = calendar.startOfDay(for: now)
    switch calendar.dateComponents([.day], from: day, to: today).day {
    case 0: return "Today \(clock)"
    case 1: return "Yesterday \(clock)"
    default: return "\(day.formatted(.dateTime.month(.abbreviated).day())), \(clock)"
    }
}

/// Milliseconds since 1970 -> "3:42 PM", in this phone's time zone.
func fmtClock(ms: Int) -> String {
    Date(timeIntervalSince1970: Double(ms) / 1000).formatted(date: .omitted, time: .shortened)
}
