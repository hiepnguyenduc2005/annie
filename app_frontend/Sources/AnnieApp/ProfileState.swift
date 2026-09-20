import SwiftUI

@MainActor
final class ProfileState: ObservableObject {
    @Published private(set) var profile: Profile?
    @Published private(set) var users: [AppUser] = []
    @Published private(set) var residents: [DogUser] = []
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    private let api: any AnnieServing
    private let store: ProfileStore

    init(api: any AnnieServing = AnnieAPI(), store: ProfileStore = ProfileStore()) {
        self.api = api
        self.store = store
        self.profile = store.load()
    }

    func loadMembers() async {
        guard !loading else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            async let family = api.users()
            async let households = api.residents()
            let (members, dogs) = try await (family, households)
            users = members
            residents = dogs
        } catch {
            self.error = connectionMessage(error)
        }
    }

    func signIn(_ user: AppUser) {
        guard let resident = residents.first(where: { $0.id == user.dog_user_id }) else {
            error = "This family member has no available resident profile."
            return
        }
        let profile = Profile(user: user, resident: resident, server: AppConfiguration.apiBaseURL.absoluteString)
        store.save(profile)
        self.profile = profile
    }

    func signOut() {
        store.clear()
        profile = nil
    }
}
