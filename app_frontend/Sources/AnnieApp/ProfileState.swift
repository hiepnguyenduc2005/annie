//
//  ProfileState.swift
//  AnnieApp
//
//  Observable wrapper around sign-in. Separate from AppState (reminders,
//  memory) because sign-in works with no backend.
//

import SwiftUI

@MainActor
final class ProfileState: ObservableObject {
    @Published private(set) var profile: Profile?

    private let store: ProfileStore

    init(store: ProfileStore = ProfileStore()) {
        self.store = store
        self.profile = store.load()
    }

    var isRegistered: Bool { profile != nil }

    func signIn(as member: FamilyMember) throws {
        let created = try ProfileRegistration.signIn(as: member, existing: profile)
        store.save(created)
        profile = created
    }

    /// Sign this device out so sign-in can be run again (e.g. it's handed to
    /// a different family member).
    func signOut() {
        store.clear()
        profile = nil
    }
}
