//
//  Profiles.swift
//  AnnieApp
//
//  Profile registration logic, with no UI. Two kinds of profile exist: the
//  app user (a person using this app) and the dog user (Annie's own profile).
//  An app user registering creates both profiles in one step; a dog user can
//  also register alone.
//
//  Storage is on this device only (UserDefaults) for now. `ProfileStore` is
//  the seam where server-side persistence plugs in later; the JSON shape of
//  `Profile` is provisional and is not yet a backend contract.
//

import Foundation

enum ProfileKind: String, Codable {
    case appUser = "app_user"
    case dogUser = "dog_user"

    var label: String {
        switch self {
        case .appUser: return "App user"
        case .dogUser: return "Dog user"
        }
    }
}

struct Profile: Codable, Identifiable, Equatable {
    let id: UUID
    let kind: ProfileKind
    var name: String
    let createdAt: Date
    /// The other profile made in the same registration, if there was one.
    var linkedProfileID: UUID?
}

enum ProfileError: Error, Equatable {
    case invalidName
    case alreadyRegistered
}

extension Array where Element == Profile {
    /// The earliest-created profile. Profiles made in one step share a
    /// timestamp, so array order (creation order) breaks the tie.
    var createdFirst: Profile? {
        enumerated().min { ($0.element.createdAt, $0.offset) < ($1.element.createdAt, $1.offset) }?.element
    }
}

enum ProfileRegistration {
    static let maxNameLength = 50

    /// Trimmed name, or nil if it's empty or too long.
    static func cleaned(_ name: String) -> String? {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return (1...maxNameLength).contains(trimmed.count) ? trimmed : nil
    }

    /// An app user registers and gets the dog user's profile made with them.
    /// Returns [app user, dog user], linked to each other. Nothing is created
    /// unless both names are valid.
    static func registerAppUser(
        name: String, dogName: String, existing: [Profile], now: Date = Date()
    ) throws -> [Profile] {
        guard existing.isEmpty else { throw ProfileError.alreadyRegistered }
        guard let name = cleaned(name), let dogName = cleaned(dogName) else { throw ProfileError.invalidName }
        let appID = UUID(), dogID = UUID()
        return [
            Profile(id: appID, kind: .appUser, name: name, createdAt: now, linkedProfileID: dogID),
            Profile(id: dogID, kind: .dogUser, name: dogName, createdAt: now, linkedProfileID: appID),
        ]
    }

    /// A dog user registers alone; there is no app user to link to yet.
    static func registerDogUser(name: String, existing: [Profile], now: Date = Date()) throws -> [Profile] {
        guard existing.isEmpty else { throw ProfileError.alreadyRegistered }
        guard let name = cleaned(name) else { throw ProfileError.invalidName }
        return [Profile(id: UUID(), kind: .dogUser, name: name, createdAt: now, linkedProfileID: nil)]
    }
}

/// On-device storage. The whole list is written as one value, so a
/// registration that makes two profiles saves both or neither.
struct ProfileStore {
    private let defaults: UserDefaults
    private let key = "annieProfiles"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load() -> [Profile] {
        guard let data = defaults.data(forKey: key) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return (try? decoder.decode([Profile].self, from: data)) ?? []
    }

    func save(_ profiles: [Profile]) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(profiles) else { return }
        defaults.set(data, forKey: key)
    }

    func clear() {
        defaults.removeObject(forKey: key)
    }
}
