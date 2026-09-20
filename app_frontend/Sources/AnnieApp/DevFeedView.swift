// Dev feed: the dog process' command center (camera with people/objects, the remembered LiDAR world,
// latency, the agent's log, buttons) inside the app, for debugging on the phone at the demo.
//
// It is a web view onto `http://<server host>:<port>/` — the same host the app's Server setting points
// at (the Mac that holds the dog link), and the port set here (default 8011, the dog process' live
// view; a simulated dog on the same Mac answers on 8111). The dog process must be started with
// `--view-host 0.0.0.0`, `ANNIE_BODY_TOKEN` set and `ANNIE_VIEW_HOSTS=<mac address>`; the page passes
// the token in `?token=` so its buttons and instructions are accepted. Nothing here is family-facing:
// the tab is hidden unless the Server setting is filled in (i.e. a dev setup).
import SwiftUI
import WebKit

struct DevFeedView: View {
    @State private var token: String = Keychain.get(.devBodyToken) ?? ""
    @State private var portText: String = String(AppConfiguration.dogFeedPort)
    @State private var reloadNonce = 0

    private var feedURL: URL? {
        let base = AppConfiguration.apiBaseURL
        guard let host = base.host else { return nil }
        var parts = URLComponents()
        parts.scheme = "http"
        parts.host = host
        parts.port = AppConfiguration.dogFeedPort
        parts.path = "/"
        if !token.isEmpty { parts.queryItems = [URLQueryItem(name: "token", value: token)] }
        return parts.url
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text("Dog feed").font(.headline)
                Spacer()
                TextField("port", text: $portText)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 70)
                    .multilineTextAlignment(.trailing)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.numberPad)
                    #endif
                    .onSubmit(apply)
                SecureField("body token", text: $token)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 180)
                    .onSubmit(apply)
                Button(action: apply) { Image(systemName: "arrow.clockwise") }
                    .accessibilityLabel("Reload the dog feed")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            if let url = feedURL {
                WebPage(url: url, nonce: reloadNonce)
                    .ignoresSafeArea(edges: .bottom)
                Text(url.host.map { "\($0):\(url.port ?? AppConfiguration.defaultDogFeedPort) — the dog process on the Mac holding the link" } ?? "")
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

    /// Save what was typed and reload. An unusable port keeps the last good one.
    private func apply() {
        token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        if token.isEmpty {
            Keychain.remove(.devBodyToken)
        } else {
            Keychain.set(token, for: .devBodyToken)
        }
        if let port = AppConfiguration.saveDogFeedPort(portText) {
            portText = String(port)
        } else {
            portText = String(AppConfiguration.dogFeedPort)
        }
        reloadNonce += 1
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
