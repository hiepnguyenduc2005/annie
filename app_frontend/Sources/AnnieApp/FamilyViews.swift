//
//  FamilyViews.swift
//  AnnieApp
//
//  One screen for everything a family member says to Annie. Most questions
//  ("did she take her medication?") are answered instantly from what Annie
//  already remembers — nothing moves. When Annie doesn't know, the answer
//  says so, and a family member can explicitly ask her to check with Jeanine
//  in person ("check if the door is shut"); that dispatches the same
//  navigate/speak/listen/recall errand as before, reported live as it happens.
//
//  The two never get confused for each other: asking is always instant and
//  silent, checking in person is always an explicit tap, one real errand,
//  reported plainly if the dog is slow, busy, or unreachable.
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
    @State private var draft = ""

    private let suggestions = [
        "Did she take her medication?",
        "Where are her glasses?",
        "Check that the front door is shut",
        "Tell her I'll call tonight",
    ]

    private var items: [ConversationItem] {
        (state.askTurns.map(ConversationItem.ask) + state.thread.map(ConversationItem.message))
            .sorted { $0.at < $1.at }
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if items.isEmpty {
                            EmptyConversationView()
                                .padding(.top, 40)
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
                    .padding(16)
                }
                .onChange(of: items.count) { _ in
                    withAnimation { proxy.scrollTo(items.last?.id, anchor: .bottom) }
                }
                .onChange(of: state.activeRun?.events.count ?? 0) { _ in
                    withAnimation { proxy.scrollTo(items.last?.id, anchor: .bottom) }
                }
            }

            Divider()
            composer
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let error = state.sendError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            if state.askTurns.isEmpty && state.thread.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(suggestions, id: \.self) { suggestion in
                            Button(suggestion) {
                                draft = suggestion
                                sendDraft()
                            }
                            .buttonStyle(.bordered)
                            .font(.callout)
                        }
                    }
                    .padding(.horizontal, 2)
                }
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Ask Annie\u{2026}", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .onSubmit(sendDraft)
                Button(action: sendDraft) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 30))
                }
                .buttonStyle(.plain)
                .foregroundStyle(canSend ? Palette.slate : Palette.mist)
                .disabled(!canSend)
                .help("Ask Annie")
            }
        }
        .padding(12)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private func sendDraft() {
        guard canSend else { return }
        let text = draft
        draft = ""
        Task { await state.ask(text) }
    }
}

private struct EmptyConversationView: View {
    var body: some View {
        VStack(spacing: 10) {
            AnnieMark(height: 52)
                .foregroundStyle(Palette.steel)
            Text("Ask Annie anything about Jeanine")
                .font(.headline)
            Text("Most questions answer instantly from what Annie's already seen. If she doesn't know, you can ask her to check with Jeanine in person.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
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
            }
        }
    }
}

private struct RunStatusChip: View {
    let run: FamilyRun

    private var color: Color {
        switch run.status {
        case "completed": return Palette.slate
        case "unreachable", "failed": return .red
        default: return Palette.steel
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            if !run.finished {
                ProgressView().controlSize(.small)
            }
            Text(run.statusLabel)
                .font(.caption.weight(.semibold))
                .foregroundStyle(color)
        }
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
                    Text(event.isAnnie ? "Annie" : "Grandma")
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
