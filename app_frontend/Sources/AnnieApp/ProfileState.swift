//
//  ProfileState.swift
//  AnnieApp
//
//  Observable wrapper around sign-in. Separate from AppState (reminders,
//  memory) because sign-in works with no backend.
//
//  Also holds the account's profile picture: a JPEG in the app's documents
//  directory, one per family member, never sent anywhere.
//

import SwiftUI

@MainActor
final class ProfileState: ObservableObject {
    @Published private(set) var profile: Profile?
    /// The signed-in member's picture, decoded once; nil when there isn't one.
    @Published private(set) var picture: CGImage?

    private let store: ProfileStore

    init(store: ProfileStore = ProfileStore()) {
        self.store = store
        self.profile = store.load()
        reloadPicture()
    }

    var isRegistered: Bool { profile != nil }

    func signIn(as member: FamilyMember) throws {
        let created = try ProfileRegistration.signIn(as: member, existing: profile)
        store.save(created)
        profile = created
        reloadPicture()
    }

    /// Sign this device out so sign-in can be run again (e.g. it's handed to
    /// a different family member).
    func signOut() {
        store.clear()
        profile = nil
        picture = nil   // the file stays: it belongs to that member, who may sign back in
    }

    // MARK: Profile picture

    /// Where a member's picture lives. On the phone this is the app's own
    /// documents directory; the Mac build has no sandbox, so it keeps out of
    /// the user's Documents folder and uses Application Support instead.
    private static func pictureURL(for member: FamilyMember) -> URL? {
        let files = FileManager.default
        #if os(iOS)
        let folder = files.urls(for: .documentDirectory, in: .userDomainMask).first
        #else
        let folder = files.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("Annie", isDirectory: true)
        #endif
        guard let folder else { return nil }
        try? files.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder.appendingPathComponent("profile-\(member.rawValue).jpg")
    }

    private func reloadPicture() {
        guard let member = profile?.member,
              let url = Self.pictureURL(for: member),
              let data = try? Data(contentsOf: url) else {
            picture = nil
            return
        }
        picture = Photo.cgImage(from: data, maxPixel: 512)
    }

    /// Shrink whatever was picked to a 512 px JPEG and keep it. Returns false,
    /// changing nothing, if it isn't an image or can't be written.
    @discardableResult
    func setPicture(from data: Data) -> Bool {
        guard let member = profile?.member,
              let url = Self.pictureURL(for: member),
              let jpeg = Photo.jpeg(from: data, maxPixel: 512, quality: 0.8) else { return false }
        do {
            try jpeg.write(to: url, options: .atomic)
        } catch {
            return false
        }
        reloadPicture()
        return picture != nil
    }

    func removePicture() {
        guard let member = profile?.member, let url = Self.pictureURL(for: member) else { return }
        try? FileManager.default.removeItem(at: url)
        picture = nil
    }
}
