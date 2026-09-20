// Dev feed: the dog process' command center (camera with people/objects, the remembered LiDAR world,
// latency, the agent's log, buttons) inside the app, for debugging on the phone at the demo.
//
// It is a web view onto `http://<server host>:8011/` — the same host the app's Server setting points at
// (the Mac that holds the dog link), port 8011 (the dog process' live view). The dog process must be
// started with `--view-host 0.0.0.0`, `ANNIE_BODY_TOKEN` set and `ANNIE_VIEW_HOSTS=<mac address>`; the
// page passes the token in `?token=` so its buttons and instructions are accepted. Nothing here is
// family-facing: the tab is hidden unless the Server setting is filled in (i.e. a dev setup).
import SwiftUI
import WebKit

struct DevFeedView: View {
    @State private var token: String = UserDefaults.standard.string(forKey: "annie.dev.bodyToken") ?? ""
    @State private var reloadNonce = 0

    private var feedURL: URL? {
        let base = AppConfiguration.apiBaseURL
        guard let host = base.host else { return nil }
        var parts = URLComponents()
        parts.scheme = "http"
        parts.host = host
        parts.port = 8011
        parts.path = "/"
        if !token.isEmpty { parts.queryItems = [URLQueryItem(name: "token", value: token)] }
        return parts.url
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text("Dog feed").font(.headline)
                Spacer()
                SecureField("body token", text: $token)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 180)
                    .onSubmit { UserDefaults.standard.set(token, forKey: "annie.dev.bodyToken"); reloadNonce += 1 }
                Button { reloadNonce += 1 } label: { Image(systemName: "arrow.clockwise") }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            if let url = feedURL {
                WebPage(url: url, nonce: reloadNonce)
                    .ignoresSafeArea(edges: .bottom)
                Text(url.host.map { "\($0):8011 — the dog process on the Mac holding the link" } ?? "")
                    .font(.caption2).foregroundStyle(.secondary).padding(.vertical, 4)
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "network").font(.largeTitle).foregroundStyle(.secondary)
                    Text("No server set").font(.headline)
                    Text("Set the Mac's address under Profile > Server first.").font(.caption).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}

#if os(iOS)
struct WebPage: UIViewRepresentable {
    let url: URL
    let nonce: Int

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        let view = WKWebView(frame: .zero, configuration: config)
        view.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        if context.coordinator.lastNonce != nonce || context.coordinator.lastURL != url {
            context.coordinator.lastNonce = nonce
            context.coordinator.lastURL = url
            view.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8))
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(url: url, nonce: nonce) }

    final class Coordinator {
        var lastURL: URL
        var lastNonce: Int
        init(url: URL, nonce: Int) { lastURL = url; lastNonce = nonce }
    }
}
#else
struct WebPage: NSViewRepresentable {
    let url: URL
    let nonce: Int

    func makeNSView(context: Context) -> WKWebView {
        let view = WKWebView(frame: .zero)
        view.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        if context.coordinator.lastNonce != nonce || context.coordinator.lastURL != url {
            context.coordinator.lastNonce = nonce
            context.coordinator.lastURL = url
            view.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8))
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(url: url, nonce: nonce) }

    final class Coordinator {
        var lastURL: URL
        var lastNonce: Int
        init(url: URL, nonce: Int) { lastURL = url; lastNonce = nonce }
    }
}
#endif
