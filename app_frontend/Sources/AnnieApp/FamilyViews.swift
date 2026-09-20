//
//  FamilyViews.swift
//  AnnieApp
//
//  One timeline for everything said to Annie, and everything Annie says.
//
//  - A question ("Where is Grandma?") is answered instantly from what Annie
//    already remembers (POST /api/ask). Nothing moves.
//  - Anything else is an instruction. While the dog is answering it goes
//    straight to her (POST /api/dog/command {text}) and the item shows the
//    mission board's receipt: queued, executing, completed or failed, with
//    her reply. When she is not, it goes as a family errand
//    (POST /api/messages) and its beats appear as the robot sends them. A
//    caption under each one says which path it took.
//  - Annie's own exchanges with whoever she meets at home arrive with the
//    dog's status every 3 s and sit in the same timeline as small cards.
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
    case instruction(DirectInstruction)
    case conversation(ConversationCard)

    var id: String {
        switch self {
        case .ask(let turn): return "ask-\(turn.id)"
        case .message(let message): return "msg-\(message.id)"
        case .instruction(let instruction): return "do-\(instruction.id)"
        case .conversation(let card): return "talk-\(card.id)"
        }
    }

    var at: Int {
        switch self {
        case .ask(let turn): return turn.at
        case .message(let message): return message.at
        case .instruction(let instruction): return instruction.at
        case .conversation(let card): return card.at
        }
    }
}

/// A first task to try, shown while the timeline is still empty.
private struct StarterTask: Identifiable {
    let title: String
    let symbol: String
    let task: String   // what is actually sent

    var id: String { task }

    static let all = [
        StarterTask(title: "Wave at Grandma", symbol: "hand.wave", task: "Go wave at Grandma"),
        StarterTask(title: "Remind her to plug in her phone", symbol: "bolt.badge.clock", task: "Tell Grandma to plug in her phone"),
        StarterTask(title: "Check on Grandma", symbol: "heart.text.square", task: "Check on Grandma"),
    ]
}

struct AskAnnieView: View {
    @EnvironmentObject private var state: AppState
    @StateObject private var dictation = SpeechDictation()
    @State private var draft = ""
    /// What was already typed when the microphone was switched on; spoken
    /// words are appended to it rather than replacing it.
    @State private var draftBeforeDictation = ""
    @FocusState private var composing: Bool
    /// The family member's own choice for the Controls card. Until they make
    /// one it is open on an empty timeline and folded once there is a
    /// conversation to read, so the chat is never squeezed into a sliver.
    @State private var controlsOpen: Bool?

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
        (state.askTurns.map(ConversationItem.ask)
            + state.thread.map(ConversationItem.message)
            + state.instructions.map(ConversationItem.instruction)
            + state.conversations.map(ConversationItem.conversation))
            .sorted { $0.at < $1.at }
    }

    private var controlsShown: Bool { controlsOpen ?? items.isEmpty }

    /// Changes whenever something on the timeline grows or moves on, so the
    /// view can follow it to the bottom.
    private var progressKey: String {
        let receipts = state.instructions.map { "\($0.state)\($0.reply == nil ? "" : "+")" }.joined(separator: ",")
        return "\(state.visibleBeatCount)|\(receipts)"
    }

    var body: some View {
        VStack(spacing: 0) {
            DogControlsCard(compact: composing || !controlsShown,
                            toggle: composing ? nil : { controlsOpen = !controlsShown })
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 10)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if items.isEmpty {
                            EmptyConversationView(disabled: state.sending) { run($0) }
                                .padding(.top, 4)
                        }
                        ForEach(items) { item in
                            Group {
                                switch item {
                                case .ask(let turn):
                                    AskTurnRow(turn: turn)
                                case .message(let message):
                                    MessageThreadItem(message: message, run: state.run(for: message),
                                                      insteadOfDog: state.fallbackRunIDs.contains(message.run_id))
                                case .instruction(let instruction):
                                    InstructionItem(instruction: instruction)
                                case .conversation(let card):
                                    AnnieConversationCard(card: card)
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
                .onChange(of: progressKey) { _ in
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
                            .disabled(state.sending || state.pausing)
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
        !draft.trimmingCharacters(in: .whitespaces).isEmpty && !state.sending && !state.pausing
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
        Task {
            await state.submit(text)
            if state.sendError != nil && draft.isEmpty {
                draft = text
            }
        }
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

/// Nothing on the timeline yet: say what this screen is for in one breath,
/// and put three real tasks one tap away.
private struct EmptyConversationView: View {
    let disabled: Bool
    let start: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Ask Annie, or send her")
                    .font(.annieHeading(22))
                    .foregroundStyle(Palette.ink)
                Text("Questions are answered from what she has seen. Anything else, she goes and does, and reports back here.")
                    .font(.footnote)
                    .foregroundStyle(Palette.steel)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Eyebrow(text: "Try it")
            HStack(alignment: .top, spacing: 10) {
                ForEach(StarterTask.all) { starter in
                    Button {
                        start(starter.task)
                    } label: {
                        VStack(alignment: .leading, spacing: 8) {
                            Image(systemName: starter.symbol)
                                .font(.system(size: 20, weight: .semibold))
                                .foregroundStyle(Palette.slate)
                                .frame(height: 24)
                            Text(starter.title)
                                .font(.footnote.weight(.semibold))
                                .foregroundStyle(Palette.ink)
                                .multilineTextAlignment(.leading)
                                .lineLimit(3)
                                .minimumScaleFactor(0.85)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                        .frame(maxWidth: .infinity, minHeight: 92, alignment: .topLeading)
                        .padding(12)
                        .background(Palette.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 16, style: .continuous)
                                .stroke(Palette.line, lineWidth: 1)
                        )
                        .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .disabled(disabled)
                    .accessibilityLabel("Try it: \(starter.title)")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The small line under something that was sent, saying which way it went.
private struct PathCaption: View {
    let symbol: String
    let text: String

    var body: some View {
        HStack(spacing: 4) {
            Spacer(minLength: 40)
            Image(systemName: symbol)
            Text(text)
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.caption2)
        .foregroundStyle(Palette.steel)
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
    /// Meant for the dog directly, but she wasn't answering when it was sent.
    var insteadOfDog = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .trailing, spacing: 4) {
                HStack {
                    Spacer(minLength: 40)
                    Text(message.text)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(Palette.slate.opacity(0.14), in: RoundedRectangle(cornerRadius: 16))
                }
                PathCaption(symbol: "tray.and.arrow.down",
                            text: insteadOfDog ? "The dog wasn't answering, so this went as a family errand"
                                               : "Sent as a family errand")
            }

            if let run {
                RunStatusChip(run: run)
                ForEach(run.events) { event in
                    RunEventRow(event: event)
                }
                if !run.finished { PauseErrandButton() }
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

private struct PauseErrandButton: View {
    @EnvironmentObject private var state: AppState
    var body: some View {
        Button {
            Task { await state.pauseTasks() }
        } label: {
            Label(state.pausing ? "Pausing…" : "Pause check-in", systemImage: "pause.circle")
        }
        .buttonStyle(.bordered)
        .disabled(state.pausing)
    }
}

/// Something sent straight to the dog, and the mission board's receipt for
/// it. The wording follows what is actually known: "accepted" and "completed"
/// are the dog's own report, not something this app observed.
private struct InstructionItem: View {
    let instruction: DirectInstruction

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .trailing, spacing: 4) {
                HStack {
                    Spacer(minLength: 40)
                    Text(instruction.text)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(Palette.slate.opacity(0.14), in: RoundedRectangle(cornerRadius: 16))
                }
                PathCaption(symbol: "bolt.fill",
                            text: instruction.simulated ? "Sent straight to Annie \u{00b7} simulator" : "Sent straight to Annie")
            }

            HStack(spacing: 6) {
                if busy {
                    ProgressView().controlSize(.mini)
                } else if let symbol {
                    Image(systemName: symbol).font(.caption2.weight(.bold))
                }
                Text(label)
                    .font(.caption.weight(.semibold))
            }
            .foregroundStyle(color)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(color.opacity(0.12), in: Capsule())

            if let reply = instruction.reply {
                HStack(alignment: .top, spacing: 8) {
                    AnnieMark(height: 16)
                        .foregroundStyle(Palette.slate)
                        .frame(width: 18)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Annie")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Text(reply)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(Palette.slate.opacity(0.14), in: RoundedRectangle(cornerRadius: 14))
            }

            if let problem {
                Text(problem)
                    .font(.footnote)
                    .foregroundStyle(Palette.alert)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var busy: Bool { !instruction.finished && !instruction.lostContact }

    private var label: String {
        if instruction.lostContact && !instruction.finished { return "Lost contact with Annie" }
        switch instruction.state {
        case "sending": return "Sending to Annie\u{2026}"
        case "queued":
            if let position = instruction.position, position > 0 { return "Queued \u{00b7} number \(position) in line" }
            return "Queued behind what she is doing"
        case "accepted": return "Annie has it"
        case "executing":
            if let total = instruction.stepsTotal, total > 0 {
                let step = min(total, (instruction.stepsDone ?? 0) + 1)
                return "Annie is on it \u{00b7} step \(step) of \(total)"
            }
            return "Annie is on it"
        case "completed": return "Annie reports it done"
        case "failed": return "Annie couldn't do it"
        case "cancelled": return "Stopped"
        case "unknown": return "Annie didn't report back on this"
        default: return instruction.state.capitalized
        }
    }

    private var symbol: String? {
        if instruction.lostContact && !instruction.finished { return "wifi.slash" }
        switch instruction.state {
        case "completed": return "checkmark"
        case "failed": return "exclamationmark.triangle.fill"
        case "cancelled": return "stop.fill"
        case "unknown": return "questionmark"
        default: return nil
        }
    }

    private var color: Color {
        if instruction.lostContact && !instruction.finished { return Palette.alert }
        switch instruction.state {
        case "completed": return Palette.slate
        case "failed": return Palette.alert
        default: return Palette.steel
        }
    }

    private var problem: String? {
        if instruction.lostContact && !instruction.finished {
            return "The dog stopped answering before this finished. It picks up here again when she is back."
        }
        guard instruction.state == "failed" || instruction.state == "cancelled" else { return nil }
        return instruction.error.map(Self.plain)
    }

    /// The board's error strings are short English already ("stopped by
    /// operator", "timed out"); they need a capital and a full stop, and any
    /// skill name in them ("find_person") read as words.
    private static func plain(_ error: String) -> String {
        let trimmed = error.replacingOccurrences(of: "_", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first else { return trimmed }
        let sentence = first.uppercased() + trimmed.dropFirst()
        return sentence.hasSuffix(".") ? sentence : sentence + "."
    }
}

/// One exchange Annie had on her own with someone at home: what she said,
/// what the microphone heard back (in quotes, because it is a transcript and
/// can be wrong), and what she answered. Red appears only for a concern.
private struct AnnieConversationCard: View {
    let card: ConversationCard

    private var exchange: DogConversation { card.exchange }
    private var who: String { exchange.name ?? "someone at home" }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                AnnieMark(height: 13)
                    .foregroundStyle(Palette.slate)
                Text("Annie \u{2194} \(who)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Palette.slate)
                    .lineLimit(1)
                Text(fmtClock(ms: card.at))
                    .font(.caption)
                    .foregroundStyle(Palette.steel)
                    .lineLimit(1)
                    .fixedSize()
                Spacer(minLength: 4)
                if exchange.isConcern {
                    Label("Concern", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption2.weight(.bold))
                        .labelStyle(.titleAndIcon)
                        .lineLimit(1)
                        .fixedSize()
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Palette.alert, in: Capsule())
                        .foregroundStyle(.white)
                }
            }

            if let asked = exchange.asked {
                line(speaker: "Annie", text: asked, quoted: false)
            }
            if let heard = exchange.heard {
                line(speaker: exchange.name ?? "Heard", text: heard, quoted: true)
            } else {
                Text("No reply heard.")
                    .font(.footnote)
                    .foregroundStyle(Palette.steel)
            }
            if let reply = exchange.reply {
                line(speaker: "Annie", text: reply, quoted: false)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.paper, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(exchange.isConcern ? Palette.alert : Palette.line, lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
    }

    private func line(speaker: String, text: String, quoted: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(speaker)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Palette.steel)
                .lineLimit(1)
                .frame(width: 52, alignment: .leading)
            Text(quoted ? "\u{201c}\(text)\u{201d}" : text)
                .font(.callout)
                .italic(quoted)
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }
}

private struct RunStatusChip: View {
    let run: FamilyRun

    private var color: Color {
        switch run.status {
        case "completed": return Palette.slate
        case "unreachable", "failed", "unknown": return Palette.alert
        default: return Palette.steel
        }
    }

    private var symbol: String? {
        switch run.status {
        case "completed": return "checkmark"
        case "cancelled", "paused": return "pause.fill"
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
