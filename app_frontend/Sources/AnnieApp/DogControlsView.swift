//
//  DogControlsView.swift
//  AnnieApp
//
//  The Controls card: a live status line from the dog and six big buttons
//  that go straight to her (POST /api/dog/command), no conversation needed.
//
//  Wording is careful about what is known. A button press that succeeds means
//  the dog process accepted the command — what she is actually doing is the
//  status line's job, refreshed every 3 s. Stop here is a software stop sent
//  over the network; it is not an emergency stop and the copy never calls it one.
//  When the dog answering is the simulated one, the card says so: a button
//  press there moves nothing in the real home.
//

import SwiftUI

struct DogControlsCard: View {
    @EnvironmentObject private var state: AppState

    /// Folded down to its status line and Stop, so the conversation keeps the
    /// screen: while the keyboard is up, and once there is a conversation.
    var compact = false

    /// When set, the header carries a chevron that folds or unfolds the card.
    var toggle: (() -> Void)?

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 10), count: 3)

    private var dogOnline: Bool { state.dog?.available == true }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 8) {
                Eyebrow(text: "Controls")
                if state.live, state.dog?.isSimulated == true {
                    SimulatorPill()
                }
                Spacer()
                if compact {
                    Button {
                        Task { await state.command(.stop) }
                    } label: {
                        Label("Stop", systemImage: DogAction.stop.symbol)
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 7)
                            .background(Palette.alert, in: Capsule())
                            .foregroundStyle(.white)
                    }
                    .buttonStyle(.plain)
                }
                if let toggle {
                    Button(action: toggle) {
                        Image(systemName: "chevron.down")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Palette.steel)
                            .rotationEffect(.degrees(compact ? 0 : 180))
                            .frame(width: 32, height: 32)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(compact ? "Show all controls" : "Hide controls")
                }
            }

            DogStatusLine(dog: state.dog, live: state.live, brief: compact)

            if !compact {
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(DogAction.allCases) { action in
                        Button {
                            Task { await state.command(action) }
                        } label: {
                            ControlButtonLabel(action: action, busy: state.commandInFlight == action)
                        }
                        .buttonStyle(ControlButtonStyle(urgent: action == .stop))
                        // Stop is always pressable: the status line can be up
                        // to 3 s stale, and stopping is the one command worth
                        // trying even when the dog looks offline.
                        .disabled(action != .stop && (!dogOnline || state.commandInFlight != nil))
                    }
                }

                if let note = state.commandNote {
                    Text(note.text)
                        .font(.footnote)
                        .foregroundStyle(note.isError ? Palette.alert : Palette.slate)
                        .fixedSize(horizontal: false, vertical: true)
                        .transition(.opacity)
                }
            }
        }
        .annieCard(padding: 14)
        .animation(.easeInOut(duration: 0.2), value: compact)
        .animation(.easeInOut(duration: 0.2), value: state.commandNote)
    }
}

/// Battery, what she is doing, who she can see, and what she remembers.
private struct DogStatusLine: View {
    let dog: DogStatus?
    let live: Bool
    /// Folded: the headline and who is in view, without what she remembers.
    var brief = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 9, height: 9)
                Text(headline)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Palette.ink)
                Spacer(minLength: 8)
                if let dog, dog.available, let battery = dog.battery {
                    Label("\(Int(battery.rounded()))%", systemImage: batterySymbol(battery))
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(battery < 20 ? Palette.alert : Palette.steel)
                        .labelStyle(.titleAndIcon)
                }
            }

            if let detail {
                Text(detail)
                    .font(.footnote)
                    .foregroundStyle(Palette.steel)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !brief, let dog, dog.available, !dog.familySentences.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Annie remembers")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Palette.slate)
                    ForEach(Array(dog.familySentences.prefix(2).enumerated()), id: \.offset) { _, sentence in
                        Text(sentence)
                            .font(.caption)
                            .foregroundStyle(Palette.steel)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .padding(.top, 2)
            }
        }
    }

    private var dotColor: Color {
        guard let dog, dog.available else { return Palette.mist }
        return dog.connected ? Palette.liveDot : Palette.steel
    }

    private var headline: String {
        guard live else { return "Not connected" }
        guard let dog else { return "Checking on Annie\u{2026}" }
        guard dog.available else { return "Dog offline" }
        guard dog.connected else { return "Waiting for the dog to connect" }
        return dog.modeLabel
    }

    private var detail: String? {
        guard live else { return "Showing demo data. Set the server under Profile, Settings, Advanced." }
        guard let dog else { return nil }
        guard dog.available else {
            return "Annie's dog isn't answering. The buttons wake up again as soon as she is back."
        }
        var parts: [String] = []
        switch dog.people ?? 0 {
        case 0: parts.append("No one in view")
        case 1: parts.append("1 person in view")
        case let n: parts.append("\(n) people in view")
        }
        if let mission = dog.missions.first(where: \.isActive) {
            let name = mission.name.replacingOccurrences(of: "_", with: " ")
            parts.append("Working on: " + (mission.detail.map { "\(name), \($0)" } ?? name))
        }
        return parts.joined(separator: " \u{00b7} ")
    }

    private func batterySymbol(_ percent: Double) -> String {
        switch percent {
        case ..<13: return "battery.0"
        case ..<38: return "battery.25"
        case ..<63: return "battery.50"
        case ..<88: return "battery.75"
        default: return "battery.100"
        }
    }
}

private struct ControlButtonLabel: View {
    let action: DogAction
    let busy: Bool

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Image(systemName: action.symbol)
                    .font(.system(size: 20, weight: .semibold))
                    .opacity(busy ? 0 : 1)
                if busy { ProgressView().controlSize(.small) }
            }
            .frame(height: 24)
            Text(action.label)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, minHeight: 66)
    }
}

/// Big, rounded, and obviously pressable. Ink on a soft fill for ordinary
/// commands; white on the alert red for Stop, the only red on the screen.
private struct ControlButtonStyle: ButtonStyle {
    let urgent: Bool
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        return configuration.label
            .foregroundStyle(urgent || pressed ? Color.white : Palette.ink)
            .background(fill(pressed: pressed), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(urgent ? Color.clear : Palette.line, lineWidth: 1)
            )
            .opacity(isEnabled ? 1 : 0.4)
            .scaleEffect(pressed ? 0.97 : 1)
            .animation(.easeOut(duration: 0.12), value: pressed)
            .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func fill(pressed: Bool) -> Color {
        if urgent { return Palette.alert.opacity(pressed ? 0.8 : 1) }
        return pressed ? Palette.slate : Palette.soft
    }
}
