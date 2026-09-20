import SwiftUI

struct ContentView: View {
    @StateObject private var controller = AudioController()

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text("Annie Audio")
                .font(.largeTitle.bold())

            VStack(alignment: .leading, spacing: 10) {
                StatusRow(title: "Server", status: controller.serverStatus)
                StatusRow(title: "Microphone", status: controller.microphone)
                StatusRow(title: "Speaker", status: controller.speaker)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(controller.connection.serverURLString)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                if let error = controller.connection.lastError, controller.connection.state != .connected {
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

            Spacer()

            VStack(spacing: 12) {
                Button {
                    controller.toggleAudio()
                } label: {
                    Text(controller.isRunning ? "Stop Audio" : "Start Audio").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(controller.isRunning ? .red : .accentColor)
                .disabled(controller.isStarting)

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
