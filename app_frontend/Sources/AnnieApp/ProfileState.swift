//
//  ProfileState.swift
//  AnnieApp
//
//  Observable wrapper around the profile registration logic. Separate from
//  AppState (reminders, memory) because registration works with no backend.
//

import SwiftUI

@MainActor
final class ProfileState: ObservableObject {
    @Published private(set) var profiles: [Profile]

    private let store: ProfileStore

    init(store: ProfileStore = ProfileStore()) {
        self.store = store
        self.profiles = store.load()
    }

    var isRegistered: Bool { !profiles.isEmpty }

    func registerAppUser(name: String, dogName: String) throws {
        let created = try ProfileRegistration.registerAppUser(name: name, dogName: dogName, existing: profiles)
        store.save(created)
        profiles = created
    }

    func registerDogUser(name: String) throws {
        let created = try ProfileRegistration.registerDogUser(name: name, existing: profiles)
        store.save(created)
        profiles = created
    }

    /// Forget this device's profiles so registration can be run again.
    func reset() {
        store.clear()
        profiles = []
    }
}
