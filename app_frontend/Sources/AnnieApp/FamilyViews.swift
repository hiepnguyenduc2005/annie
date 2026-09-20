//
//  FamilyViews.swift
//  AnnieApp
//
//  The family side: what Zach sees from two hours away. He writes to Annie,
//  Annie walks it over to Jeanine, and each beat of that errand comes back
//  here as it happens.
//
//  Sending never waits on the robot. A message is accepted immediately and
//  the errand is reported afterwards, so a dog that is busy, slow or switched
//  off leaves the app responsive and says so plainly.
//

import SwiftUI

struct FamilyView: View {
    @EnvironmentObject private var state: AppState
    @State private var draft = ""

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if state.thread.isEmpty {
                            EmptyThreadView()
                                .padding(.top, 40)
                        }
                        ForEach(state.thread) { message in
                            MessageThreadItem(message: message, run: state.run(for: message))
                                .id(message.id)
                        }
                    }
                    .padding(16)
                }
                .onChange(of: state.thread.count) { _ in
                    withAnimation { proxy.scrollTo(state.thread.last?.id, anchor: .bottom) }
                }
                .onChange(of: state.activeRun?.events.count ?? 0) { _ in
                    withAnimation { proxy.scrollTo(state.thread.last?.id, anchor: .bottom) }
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
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Ask Annie to tell Grandma\u{2026}", text: $draft, axis: .vertical)
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
                .help("Send through Annie")
            }
        }
        .padding(12)
    }

    private var canSend: Bool {
        !state.sending && !draft.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private func sendDraft() {
        guard canSend else { return }
        let text = draft
        draft = ""
        Task { await state.send(text) }
    }
}

private struct EmptyThreadView: View {
    var body: some View {
        VStack(spacing: 10) {
            AnnieMark(height: 52)
                .foregroundStyle(Palette.steel)
            Text("Send Grandma a message")
                .font(.headline)
            Text("Annie will find her, pass it along, and tell you what she says back.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
    }
}

/// One sent message plus the errand it kicked off.
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
