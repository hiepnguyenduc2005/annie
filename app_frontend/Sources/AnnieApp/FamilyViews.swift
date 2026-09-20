import SwiftUI

struct AskAnnieView: View {
    @EnvironmentObject private var state: AppState
    @State private var draft = ""
    @FocusState private var composerFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Ask Annie").font(.title2.bold())
                    Text("Today's conversation · \(state.residentName)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                RemoteMuteControl()
            }.padding(16)
            SectionError(text: state.messageError)
            ScrollViewReader { reader in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if state.messages.isEmpty && !state.loading {
                            Text("Send Annie a message or ask about \(state.residentName). Replies will appear here.")
                                .foregroundStyle(.secondary).padding(.vertical, 30)
                        }
                        ForEach(state.messages) { message in
                            HStack {
                                if message.role == "app_user" { Spacer(minLength: 30) }
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(message.role == "resident" ? state.residentName : message.speaker)
                                        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                                    Text(message.text).textSelection(.enabled)
                                    HStack {
                                        Text(displayTimestamp(message.timestamp, timezone: state.timezone, date: false))
                                        if let status = message.status { Text("· \(statusLabel(status))") }
                                    }.font(.caption).foregroundStyle(.secondary)
                                }
                                .padding(12)
                                .background(message.role == "app_user" ? Palette.mist.opacity(0.3) : Palette.steel.opacity(0.12),
                                            in: RoundedRectangle(cornerRadius: 14))
                                if message.role != "app_user" { Spacer(minLength: 30) }
                            }.id(message.id)
                        }
                    }.padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await state.refreshConversation() }
                .onChange(of: state.messages.last?.id) { id in
                    if let id { withAnimation { reader.scrollTo(id, anchor: .bottom) } }
                }
            }
            Divider()
            SectionError(text: state.sendError)
            if composerFocused {
                HStack {
                    Spacer()
                    Button {
                        composerFocused = false
                    } label: {
                        Label("Hide keyboard", systemImage: "keyboard.chevron.compact.down")
                            .font(.subheadline.weight(.semibold))
                            .frame(minHeight: 44)
                    }
                    .accessibilityHint("Shows the tab menu and keeps your draft")
                }
                .padding(.horizontal, 12)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Message Annie…", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(1...5).disabled(state.sending)
                    .focused($composerFocused)
                Button {
                    let submitted = draft
                    Task {
                        if await state.send(submitted), draft == submitted { draft = "" }
                    }
                } label: {
                    if state.sending { ProgressView() }
                    else { Image(systemName: "arrow.up.circle.fill").font(.title) }
                }
                .accessibilityLabel("Send message")
                .disabled(state.sending || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || draft.count > 4000)
            }.padding(12)
        }
        .onDisappear { composerFocused = false }
    }
}


struct RemoteVoiceState: Decodable {
    let muted: Bool?
}

struct RemoteVoiceUpdate: Encodable {
    let muted: Bool
}

/// The confirmed robot/Mac speaker state; independent of conversation and microphone.
private struct RemoteMuteControl: View {
    @State private var muted: Bool?
    @State private var saving = false
    @State private var error: String?
    @State private var revision = UUID()
    private let api = AnnieAPI()

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Button {
                Task { await toggle() }
            } label: {
                Label(saving ? "Saving…" : muted == true ? "Unmute" : muted == false ? "Mute" : "Audio unavailable",
                      systemImage: muted == true ? "speaker.slash.fill" : "speaker.wave.2.fill")
                    .font(.subheadline.weight(.semibold))
                    .frame(minHeight: 44)
            }
            .disabled(saving || muted == nil)
            .accessibilityHint("Controls robot and Mac speech; microphone stays on")
            if let error {
                Text(error).font(.caption).foregroundStyle(.red).frame(maxWidth: 170)
            }
        }
        .task {
            while !Task.isCancelled {
                await refresh()
                do { try await Task.sleep(nanoseconds: 3_000_000_000) } catch { break }
            }
        }
    }

    @MainActor private func refresh() async {
        guard !saving else { return }
        let current = revision
        do {
            let response = try await api.voiceSettings()
            guard !saving, revision == current else { return }
            muted = response.muted
        } catch {
            guard !saving, revision == current else { return }
            muted = nil
        }
    }

    @MainActor private func toggle() async {
        guard let previous = muted, !saving else { return }
        saving = true
        revision = UUID()
        error = nil
        defer { saving = false }
        do {
            let response = try await api.setMuted(!previous)
            muted = response.muted
            if response.muted != !previous { error = "Audio change was not confirmed." }
        } catch {
            muted = nil
            self.error = "Audio change was not confirmed. Please retry when available."
        }
    }
}
