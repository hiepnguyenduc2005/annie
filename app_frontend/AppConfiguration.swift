//
//  AppConfiguration.swift
//  Annie
//
//  App-level configuration. The single place that knows where the
//  Annie Companion API lives — every API call goes through
//  `AppConfiguration.apiURL(...)`, so nothing else ever declares
//  the local host.
//

import Foundation

enum AppConfiguration {
    /// Where the API server is by default: the Swift backend in
    /// `app_frontend/backend/main.swift`, served from your machine.
    ///
    /// Override per-run (e.g. demo machine, deployed server) with the
    /// `ANNIE_API_URL` environment variable — no code change needed.
    static let apiBaseURL: URL = {
        if let override = ProcessInfo.processInfo.environment["ANNIE_API_URL"],
           let url = URL(string: override) {
            return url
        }
        return URL(string: "http://127.0.0.1:8000")!
    }()

    /// Build an endpoint URL from a path like `"api/reminders"`.
    /// Leading slashes are fine: `apiURL("/api/reminders")` works too.
    static func apiURL(_ path: String) -> URL {
        let clean = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return apiBaseURL.appending(path: clean)
    }
}
