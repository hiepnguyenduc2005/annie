import SwiftUI

struct ContentView: View {
    @StateObject private var controller = AudioController()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text("Annie Audio")
                .font(.largeTitle.bold())

            VStack(alignment: .leading, spacing: 10) {
                StatusRow(title: "Server", status: controller.serverStatus)
                StatusRow(title: "Microphone", status: controller.microphone)
                StatusRow(title: "Speaker", status: controller.speaker)
                StatusRow(title: "Conversation", status: controller.conversationStatus)
            }

            VStack(alignment: .leading, spacing: 4) {
                if let error = controller.connection.lastError {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
                if controller.microphoneDenied {
                    Text("Microphone access is off. Enable it for Annie Audio in Settings to send audio.")
                        .font(.caption).foregroundStyle(.red)
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                    .font(.caption)
                }
            }

            // Byte counters change ~50x/s; sample them twice a second.
            TimelineView(.periodic(from: .now, by: 0.5)) { _ in
                let connection = controller.connection
                VStack(alignment: .leading, spacing: 6) {
                    Text("Outgoing: \(Self.bytes(connection.bytesSent)) · \(connection.packetsSent) packets")
                    Text("Incoming: \(Self.bytes(connection.bytesReceived)) · \(connection.packetsReceived) packets")
                    if connection.packetsDropped > 0 {
                        Text("Dropped (network busy): \(connection.packetsDropped)").foregroundStyle(.orange)
                    }
                }
                .font(.callout.monospacedDigit())
            }

            Text("While this app is open, Annie can start a conversation automatically. She speaks first, then listens for your reply.")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()

            VStack(spacing: 12) {
                Button {
                    controller.toggleAudio()
                } label: {
                    Text(controller.isRunning ? "Stop Audio" : "Start Audio").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(controller.isRunning ? .red : .accentColor)
                .disabled(controller.isStarting || controller.connection.state != .connected)

                Button {
                    controller.toggleConnection()
                } label: {
                    Text(controller.isConnectionActive ? "Disconnect" : "Connect").frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
            }
            .controlSize(.large)
        }
        .padding(24)
        .task { controller.foreground() }
        .onChange(of: scenePhase) { phase in
            if phase == .active { controller.foreground() }
            else if phase == .background { controller.background() }
        }
    }

    private static func bytes(_ count: Int) -> String {
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        formatter.allowsNonnumericFormatting = false   // "0 KB", not "Zero KB"
        return formatter.string(fromByteCount: Int64(count))
    }
}

private struct StatusRow: View {
    let title: String
    let status: StatusLine

    var body: some View {
        HStack(spacing: 8) {
            Text("\(title):").foregroundStyle(.secondary)
            Circle().fill(color).frame(width: 10, height: 10)
            Text(status.text)
        }
        .font(.body)
    }

    private var color: Color {
        switch status.level {
        case .off: return .gray
        case .ok: return .green
        case .warning: return .orange
        case .error: return .red
        }
    }
}

#Preview {
    ContentView()
}
