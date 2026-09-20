//
//  Profiles.swift
//  AnnieApp
//
//  Sign-in, not registration: the household's family members are fixed
//  (`app_backend`'s `HOUSEHOLD` accepts exactly `zach` and `ellis` as message
//  authors), so this just remembers which one owns this phone. No password —
//  the phone itself is the credential, same as before.
//
//  Storage is on this device only (UserDefaults) for now. `ProfileStore` is
//  the seam where server-side persistence plugs in later.
//

import Foundation

/// A family member who can sign in to this app. Matches `HOUSEHOLD` in
/// `app_backend/app/family.py` exactly; adding a family member here without
/// adding them there will be rejected by `POST /api/messages`.
enum FamilyMember: String, Codable, CaseIterable, Identifiable {
    case zach
    case ellis

    var id: String { rawValue }
    var displayName: String {
        switch self {
        case .zach: return "Zach"
        case .ellis: return "Ellis"
        }
    }
}

struct Profile: Codable, Equatable {
    let member: FamilyMember
    let createdAt: Date
}

enum ProfileError: Error, Equatable {
    case alreadyRegistered
}

enum ProfileRegistration {
    /// Sign this phone in as `member`. Nothing happens if it's already signed in.
    static func signIn(as member: FamilyMember, existing: Profile?, now: Date = Date()) throws -> Profile {
        guard existing == nil else { throw ProfileError.alreadyRegistered }
        return Profile(member: member, createdAt: now)
    }
}

/// On-device storage.
struct ProfileStore {
    private let defaults: UserDefaults
    private let key = "annieProfile"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load() -> Profile? {
        guard let data = defaults.data(forKey: key) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(Profile.self, from: data)
    }

    func save(_ profile: Profile) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(profile) else { return }
        defaults.set(data, forKey: key)
    }

    func clear() {
        defaults.removeObject(forKey: key)
    }
}
