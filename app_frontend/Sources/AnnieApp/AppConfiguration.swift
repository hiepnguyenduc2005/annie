//
//  AppConfiguration.swift
//  Annie
//
//  App-level configuration. The single place that knows where the
//  Annie Companion API lives — every API call goes through
//  `AppConfiguration.apiBaseURL` / `apiURL(...)`, so nothing else ever
//  declares the host.
//
//  Resolution order (first match wins):
//    1. `ANNIE_API_URL` environment variable — per-run override for the Mac
//       build (`ANNIE_API_URL=http://host:8000 swift run AnnieApp`).
//    2. The address saved in the app's Profile tab (UserDefaults). This is how
//       a phone points at the Mac running the backend, since `127.0.0.1` on a
//       phone is the phone itself.
//    3. `http://127.0.0.1:8000` — the backend on the same machine.
//

import Foundation

enum AppConfiguration {
    static let defaultAPIBaseURL = URL(string: "http://127.0.0.1:8000")!
    private static let savedURLKey = "annieAPIBaseURL"
    private static let savedTokenKey = "annieAPIToken"

    /// Bearer token for the backend, when one is configured there. Empty means
    /// none, which the backend only accepts from loopback clients — so a phone
    /// on the LAN needs this set. Kept in UserDefaults alongside the address:
    /// this is a demo credential for a local network, not a secret store.
    static var apiToken: String {
        if let fromEnvironment = ProcessInfo.processInfo.environment["ANNIE_API_TOKEN"], !fromEnvironment.isEmpty {
            return fromEnvironment
        }
        return UserDefaults.standard.string(forKey: savedTokenKey) ?? ""
    }

    static func saveAPIToken(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            UserDefaults.standard.removeObject(forKey: savedTokenKey)
        } else {
            UserDefaults.standard.set(trimmed, forKey: savedTokenKey)
        }
    }

    /// Set when `ANNIE_API_URL` is present and valid; it beats the saved address.
    static var environmentOverride: URL? {
        ProcessInfo.processInfo.environment["ANNIE_API_URL"].flatMap(normalizedURL)
    }

    /// Where the API server is right now.
    static var apiBaseURL: URL {
        if let url = environmentOverride { return url }
        if let saved = UserDefaults.standard.string(forKey: savedURLKey), let url = normalizedURL(saved) {
            return url
        }
        return defaultAPIBaseURL
    }

    /// Save the server address typed into the app. Returns the URL that will be
    /// used, or nil (and saves nothing) if the text isn't a usable http(s) address.
    /// Empty text clears the saved address and returns to the default.
    @discardableResult
    static func saveAPIBaseURL(_ text: String) -> URL? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            UserDefaults.standard.removeObject(forKey: savedURLKey)
            return apiBaseURL
        }
        guard let url = normalizedURL(trimmed) else { return nil }
        UserDefaults.standard.set(url.absoluteString, forKey: savedURLKey)
        return apiBaseURL
    }

    /// Accepts `192.168.1.20:8000` or `http://192.168.1.20:8000/`; requires an
    /// http(s) scheme and a host, and defaults a missing scheme to http.
    static func normalizedURL(_ text: String) -> URL? {
        var candidate = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !candidate.isEmpty else { return nil }
        if !candidate.contains("://") { candidate = "http://" + candidate }
        while candidate.hasSuffix("/") { candidate.removeLast() }
        guard let url = URL(string: candidate),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty else { return nil }
        return url
    }

    /// Build an endpoint URL from a path like `"api/reminders"`.
    /// Leading slashes are fine: `apiURL("/api/reminders")` works too.
    static func apiURL(_ path: String) -> URL {
        let clean = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return apiBaseURL.appending(path: clean)
    }
}
