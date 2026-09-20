//
//  FamilyViews.swift
//  AnnieApp
//
//  One screen for everything a family member says to Annie.
//
//  - A question ("Where is Grandma?") is answered instantly from what Annie
//    already remembers (POST /api/ask). Nothing moves.
//  - Anything else ("Go wave at Grandma") is an instruction and becomes a
//    mission (POST /api/messages): she finds the person, speaks, listens, and
//    reports. Its beats appear under the message as the robot sends them.
//  - The Controls card on top talks to the dog directly (DogControlsView).
//
//  `AppState.submit` decides which path a line of text takes. When Annie has
//  no answer to a question, the turn offers one explicit tap to go and check
//  in person. Failures are always said in words: busy, offline, unreachable.
//

import SwiftUI

/// Either kind of thing that can appear in the conversation, in the order it
/// happened.
private enum ConversationItem: Identifiable {
    case ask(AskTurn)
    case message(ThreadMessage)

    var id: String {
        switch self {
        case .ask(let turn): return "ask-\(turn.id)"
        case .message(let message): return "msg-\(message.id)"
        }
    }

    var at: Int {
        switch self {
        case .ask(let turn): return turn.at
        case .message(let message): return message.at
        }
    }
}

struct AskAnnieView: View {
    @EnvironmentObject private var state: AppState
    @StateObject private var dictation = SpeechDictation()
    @State private var draft = ""
    /// What was already typed when the microphone was switched on; spoken
    /// words are appended to it rather than replacing it.
    @State private var draftBeforeDictation = ""
    @FocusState private var composing: Bool

    /// Real end-to-end demo tasks, always one tap away. The question is
    /// answered from memory; the rest are missions for the dog.
    private let tasks = [
        "Go wave at Grandma",
        "Tell Grandma to plug in her phone",
        "Check on Grandma",
        "Where is Grandma?",
        "Go explore",
        "Come home",
    ]

    private var items: [ConversationItem] {
        (state.askTurns.map(ConversationItem.ask) + state.thread.map(ConversationItem.message))
            .sorted { $0.at < $1.at }
    }

    var body: some View {
        VStack(spacing: 0) {
            DogControlsCard(compact: composing)
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 10)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if items.isEmpty {
                            EmptyConversationView()
                                .padding(.top, 12)
                        }
                        ForEach(items) { item in
                            Group {
                                switch item {
                                case .ask(let turn):
                                    AskTurnRow(turn: turn)
                                case .message(let message):
                                    MessageThreadItem(message: message, run: state.run(for: message))
                                }
                            }
                            .id(item.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
                }
                #if os(iOS)
                .scrollDismissesKeyboard(.interactively)
                #endif
                .onChange(of: items.count) { _ in
                    withAnimation { proxy.scrollTo(items.last?.id, anchor: .bottom) }
                }
                .onChange(of: state.visibleBeatCount) { _ in
                    withAnimation { proxy.scrollTo(items.last?.id, anchor: .bottom) }
                }
            }

            Rectangle().fill(Palette.line).frame(height: 1)
            composer
        }
        .background(Palette.cream)
        .onChange(of: dictation.transcript) { spoken in
            guard dictation.isRecording || !spoken.isEmpty else { return }
            draft = draftBeforeDictation.isEmpty ? spoken : draftBeforeDictation + " " + spoken
        }
        .onDisappear { dictation.stop() }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let problem = dictation.problem ?? state.sendError {
                Text(problem)
                    .font(.footnote)
                    .foregroundStyle(Palette.alert)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(tasks, id: \.self) { task in
                        Button(task) { run(task) }
                            .buttonStyle(TaskChipStyle())
                            .disabled(state.sending)
                    }
                }
                .padding(.horizontal, 12)
            }

            HStack(alignment: .bottom, spacing: 8) {
                if dictation.isSupported {
                    Button(action: toggleDictation) {
                        Image(systemName: dictation.isRecording ? "stop.circle.fill" : "mic.circle.fill")
                            .font(.system(size: 32))
                            .symbolRenderingMode(.hierarchical)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(dictation.isRecording ? Palette.alert : Palette.slate)
                    .accessibilityLabel(dictation.isRecording ? "Stop listening" : "Speak to Annie")
                }

                TextField(dictation.isRecording ? "Listening\u{2026}" : "Ask or tell Annie\u{2026}", text: $draft, axis: .vertical)
                    .lineLimit(1...4)
                    .focused($composing)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(Palette.paper, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .stroke(dictation.isRecording ? Palette.alert : Palette.line, lineWidth: 1)
                    )
                    .onSubmit(sendDraft)

                Button(action: sendDraft) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 32))
                }
                .buttonStyle(.plain)
                .foregroundStyle(canSend ? Palette.slate : Palette.mist)
                .disabled(!canSend)
                .accessibilityLabel("Send to Annie")
            }
            .padding(.horizontal, 12)
        }
        .padding(.vertical, 10)
        .background(Palette.cream)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespaces).isEmpty && !state.sending
    }

    private func toggleDictation() {
        if !dictation.isRecording { draftBeforeDictation = draft.trimmingCharacters(in: .whitespaces) }
        dictation.toggle()
    }

    private func sendDraft() {
        guard canSend else { return }
        dictation.stop()
        let text = draft
        draft = ""
        draftBeforeDictation = ""
        composing = false
        Task { await state.submit(text) }
    }

    private func run(_ task: String) {
        dictation.stop()
        composing = false
        Task { await state.submit(task) }
    }
}

/// A sample task: soft capsule, ink text, slate when pressed.
private struct TaskChipStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.medium))
            .lineLimit(1)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .foregroundStyle(configuration.isPressed ? Color.white : Palette.ink)
            .background(configuration.isPressed ? Palette.slate : Palette.paper, in: Capsule())
            .overlay(Capsule().stroke(Palette.line, lineWidth: 1))
            .opacity(isEnabled ? 1 : 0.5)
    }
}

private struct EmptyConversationView: View {
    var body: some View {
        VStack(spacing: 8) {
            AnnieMark(height: 40)
                .foregroundStyle(Palette.steel)
            Text("Ask Annie, or send her")
                .font(.annieHeading(22))
                .foregroundStyle(Palette.ink)
            Text("Questions are answered straight away from what Annie has seen. Anything else becomes an errand: she finds Grandma, says it, listens, and reports back here.")
                .font(.callout)
                .foregroundStyle(Palette.steel)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 20)
    }
}

/// A question and its instant answer. Never touches the robot — if Annie
/// doesn't know, the fallback answer says so and offers the one explicit way
/// to make her actually go check.
private struct AskTurnRow: View {
    @EnvironmentObject private var state: AppState
    let turn: AskTurn
    @State private var escalated = false

    var body: some View {
        VStack(alignment: .trailing, spacing: 10) {
            HStack {
                Spacer(minLength: 40)
                Text(turn.question)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Palette.slate.opacity(0.14), in: RoundedRectangle(cornerRadius: 16))
            }

            if let answer = turn.answer {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .top, spacing: 8) {
                        AnnieMark(height: 16)
                            .foregroundStyle(Palette.slate)
                            .padding(.top, 2)
                        Text(answer)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        Button {
                            state.speak(answer)
                        } label: {
                            Image(systemName: "speaker.wave.2")
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .help("Read the answer aloud")
                    }

                    if turn.hasNoAnswer && !escalated {
                        Button("Have Annie check with Jeanine in person") {
                            escalated = true
                            Task { await state.send(turn.question) }
                        }
                        .font(.caption.weight(.semibold))
                        .buttonStyle(.bordered)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.mist.opacity(0.14), in: RoundedRectangle(cornerRadius: 14))
            } else {
                HStack(spacing: 8) {
                    AnnieMark(height: 16).foregroundStyle(Palette.slate)
                    ProgressView().controlSize(.small)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.mist.opacity(0.14), in: RoundedRectangle(cornerRadius: 14))
            }
        }
    }
}

/// One "check in person" errand: the message that kicked it off, plus its
/// live progress as Annie reports it.
private struct MessageThreadItem: View {
    let message: ThreadMessage
    let run: FamilyRun?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Spacer(minLength: 40)
                Text(message.text)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Palette.slate.opacity(0.14), in: RoundedRectangle(cornerRadius: 16))
            }

            if let run {
                RunStatusChip(run: run)
                ForEach(run.events) { event in
                    RunEventRow(event: event)
                }
            } else {
                // Sent, but the run hasn't been read back yet.
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Sending to Annie\u{2026}")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Palette.steel)
                }
            }
        }
    }
}

private struct RunStatusChip: View {
    let run: FamilyRun

    private var color: Color {
        switch run.status {
        case "completed": return Palette.slate
        case "unreachable", "failed": return Palette.alert
        default: return Palette.steel
        }
    }

    private var symbol: String? {
        switch run.status {
        case "completed": return "checkmark"
        case "unreachable", "failed": return "exclamationmark.triangle.fill"
        default: return nil
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            if !run.finished {
                ProgressView().controlSize(.mini)
            } else if let symbol {
                Image(systemName: symbol).font(.caption2.weight(.bold))
            }
            Text(run.statusLabel)
                .font(.caption.weight(.semibold))
        }
        .foregroundStyle(color)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(color.opacity(0.12), in: Capsule())
    }
}

/// A single beat. Annie's and Jeanine's lines read as speech; the rest is
/// quiet progress text, so the conversation stands out from the logistics.
private struct RunEventRow: View {
    let event: RunEvent

    var body: some View {
        if event.isAnnie || event.isResident {
            HStack(alignment: .top, spacing: 8) {
                Group {
                    if event.isAnnie {
                        AnnieMark(height: 16)
                    } else {
                        Image(systemName: "person.fill")
                    }
                }
                .foregroundStyle(event.isAnnie ? Palette.slate : Palette.steel)
                .frame(width: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text(event.isAnnie ? "Annie" : "Jeanine")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(event.summary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(
                (event.isAnnie ? Palette.slate : Palette.mist).opacity(0.14),
                in: RoundedRectangle(cornerRadius: 14)
            )
        } else {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 18)
                Text(event.summary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
        }
    }

    private var icon: String {
        switch event.kind {
        case "navigating": return "figure.walk"
        case "arrived": return "mappin.and.ellipse"
        case "listening": return "ear"
        case "recalling": return "brain"
        case "recalled": return "lightbulb"
        case "completed": return "checkmark.circle"
        case "unreachable", "failed": return "exclamationmark.triangle"
        default: return "circle"
        }
    }
}
