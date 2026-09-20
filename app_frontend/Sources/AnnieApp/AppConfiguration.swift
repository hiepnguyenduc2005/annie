import Foundation

/// Build-configured connection, with environment overrides for Mac development.
/// Old addresses and tokens saved on the phone are intentionally ignored.
enum AppConfiguration {
    static let defaultAPIBaseURL = URL(string: "http://127.0.0.1:8000")!

    private static func setting(_ environmentKey: String, _ bundleKey: String) -> String? {
        let value = ProcessInfo.processInfo.environment[environmentKey]
            ?? (Bundle.main.object(forInfoDictionaryKey: bundleKey) as? String)
        guard let text = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty, !text.contains("$(") else { return nil }
        return text
    }

    static var apiBaseURL: URL {
        setting("ANNIE_API_URL", "AnnieAPIURL").flatMap(normalizedURL) ?? defaultAPIBaseURL
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
