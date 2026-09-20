import Foundation

struct Profile: Codable, Equatable {
    let user: AppUser
    let resident: DogUser
    let server: String
}

struct ProfileStore {
    let defaults: UserDefaults
    private let key = "annie.profile.v2"

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    func load() -> Profile? {
        guard let data = defaults.data(forKey: key), let profile = try? JSONDecoder().decode(Profile.self, from: data),
              profile.server == AppConfiguration.apiBaseURL.absoluteString else { return nil }
        return profile
    }

    func save(_ profile: Profile) {
        defaults.set(try? JSONEncoder().encode(profile), forKey: key)
    }

    func clear() { defaults.removeObject(forKey: key) }
}
