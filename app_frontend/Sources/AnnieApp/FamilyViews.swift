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
