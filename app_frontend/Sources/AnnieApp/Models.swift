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

    var id: String { run_id }

    /// True once the robot can send nothing further for this run.
    var finished: Bool { ["completed", "failed", "unreachable"].contains(status) }

    var statusLabel: String {
        switch status {
        case "dispatched": return "Sending to Annie\u{2026}"
        case "running": return "Annie is on it"
        case "completed": return "Delivered"
        case "failed": return "Annie couldn't finish"
        case "unreachable": return "Couldn't reach Annie"
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

/// One mission receipt from the dog's mission board.
struct DogMission: Decodable, Identifiable, Equatable {
    let command_id: String
    let name: String
    let state: String
    let detail: String?   // args.text / args.thing, when the mission has one

    var id: String { command_id }

    private enum Keys: String, CodingKey { case command_id, name, state, args }
    private enum ArgKeys: String, CodingKey { case text, thing, person }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = (try? c.decode(String.self, forKey: .name)) ?? "mission"
        state = (try? c.decode(String.self, forKey: .state)) ?? ""
        command_id = (try? c.decode(String.self, forKey: .command_id)) ?? UUID().uuidString
        let args = try? c.nestedContainer(keyedBy: ArgKeys.self, forKey: .args)
        detail = (try? args?.decode(String.self, forKey: .text))
            ?? (try? args?.decode(String.self, forKey: .thing))
            ?? (try? args?.decode(String.self, forKey: .person))
    }

    var isActive: Bool { state == "accepted" || state == "executing" }
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

    static let offline = DogStatus(available: false, connected: false, mode: nil, action: nil, battery: nil,
                                   people: nil, greetings: nil, checkins: nil, missions: [], sentences: [])

    private enum Keys: String, CodingKey {
        case available, connected, mode, action, battery, people, greetings, checkins, missions, sentences
    }

    init(available: Bool, connected: Bool, mode: String?, action: String?, battery: Double?, people: Int?,
         greetings: Int?, checkins: Int?, missions: [DogMission], sentences: [String]) {
        self.available = available; self.connected = connected; self.mode = mode; self.action = action
        self.battery = battery; self.people = people; self.greetings = greetings; self.checkins = checkins
        self.missions = missions; self.sentences = sentences
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
    }

    /// The patrol loop's mode in plain words. It reports "cruise", "follow",
    /// "voice_stop", planner states such as "blocked", and "brain:<action>"
    /// when the situated agent is steering; the prefix is dropped.
    var modeLabel: String {
        let raw = (mode ?? "").lowercased()
        let key = raw.split(separator: ":").last.map(String.init) ?? raw
        switch key {
        case "", "-", "idle", "stop", "stopped": return "Standing by"
        case "cruise", "explore", "exploring", "wander": return "Exploring"
        case "follow": return "Following someone"
        case "approach": return "Walking up to someone"
        case "blocked": return "Finding a way round"
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
        case available, cloud, elevenlabs, deepgram, speak_via, hear_via, devices, input, output
    }
    private enum DeviceKeys: String, CodingKey { case devices, input, output }

    init(available: Bool, cloud: Bool, elevenlabs: Bool, deepgram: Bool, speak_via: String, hear_via: String,
         devices: [AudioDevice], input: String?, output: String?) {
        self.available = available; self.cloud = cloud; self.elevenlabs = elevenlabs; self.deepgram = deepgram
        self.speak_via = speak_via; self.hear_via = hear_via; self.devices = devices
        self.input = input; self.output = output
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
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
