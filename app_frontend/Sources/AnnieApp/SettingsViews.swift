//
//  SettingsViews.swift
//  AnnieApp
//
//  Profile > Settings: how Annie speaks and hears, which microphone and
//  speaker she uses, the cloud voice keys, and (under Advanced) where her
//  server lives.
//
//  Everything here acts on the machine that runs the dog, through
//  GET/POST /api/settings/voice. Two rules shape the code:
//
//  - Keys are write-only. The server only ever says whether one is set. On
//    this phone they live in the Keychain and nowhere else, and they are sent
//    when the family member presses Save — never silently in the background.
//  - An empty key CLEARS the dog's key, so a key is only ever sent when
//    something was actually typed, and clearing is its own explicit button.
//

import SwiftUI

struct SettingsSectionView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Settings")
                .font(.annieHeading(22))
                .foregroundStyle(Palette.ink)

            VoiceSettingsCard()
            AdvancedSettingsCard()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// ---------------------------------------------------------------------------
// Voice
// ---------------------------------------------------------------------------

private struct VoiceSettingsCard: View {
    @EnvironmentObject private var state: AppState

    private var voice: VoiceSettings? { state.voice }
    private var usable: Bool { voice?.available == true && !state.voiceSaving }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Eyebrow(text: "Voice")

            Toggle(isOn: cloudBinding) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Cloud voice")
                        .font(.body.weight(.semibold))
                    Text("ElevenLabs speaks, Deepgram hears")
                        .font(.footnote)
                        .foregroundStyle(Palette.steel)
                }
            }
            .tint(Palette.slate)
            .disabled(!usable)

            Text(summary)
                .font(.footnote)
                .foregroundStyle(voice?.available == true ? Palette.slate : Palette.steel)
                .fixedSize(horizontal: false, vertical: true)

            if let voice, voice.available {
                Rectangle().fill(Palette.line).frame(height: 1)

                DevicePickerRow(title: "Microphone", symbol: "mic",
                                devices: voice.microphones, current: voice.input, enabled: usable) { name in
                    Task {
                        await state.updateVoice(VoiceSettingsUpdate(input_device: name),
                                                saved: name.isEmpty ? "Annie is using the computer's default microphone."
                                                                    : "Annie is now listening through \(name).")
                    }
                }
                DevicePickerRow(title: "Speaker", symbol: "speaker.wave.2",
                                devices: voice.speakers, current: voice.output, enabled: usable) { name in
                    Task {
                        await state.updateVoice(VoiceSettingsUpdate(output_device: name),
                                                saved: name.isEmpty ? "Annie is using the computer's default speaker."
                                                                    : "Annie is now speaking through \(name).")
                    }
                }

                Rectangle().fill(Palette.line).frame(height: 1)

                KeyRow(title: "ElevenLabs key", account: .elevenLabs, dogHasKey: voice.elevenlabs, enabled: usable)
                KeyRow(title: "Deepgram key", account: .deepgram, dogHasKey: voice.deepgram, enabled: usable)

                Text("Keys are kept in this phone's Keychain and sent to Annie only when you press Save. Annie holds them in memory and never shows them back.")
                    .font(.caption)
                    .foregroundStyle(Palette.steel)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let note = state.voiceNote {
                Text(note.text)
                    .font(.footnote)
                    .foregroundStyle(note.isError ? Palette.alert : Palette.slate)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .annieCard()
        .task { await state.loadVoiceSettings() }
    }

    /// Reads the dog's answer, writes through to the dog. There is no local
    /// copy to drift: if the change is refused, the switch falls back by itself.
    private var cloudBinding: Binding<Bool> {
        Binding(
            get: { voice?.cloud ?? false },
            set: { on in
                Task {
                    await state.updateVoice(VoiceSettingsUpdate(cloud: on),
                                            saved: on ? "Cloud voice is on." : "Cloud voice is off. Annie is using the voice built into the computer.")
                }
            }
        )
    }

    private var summary: String {
        guard state.live else { return "Not connected to Annie. Set the server under Advanced." }
        guard let voice else { return "Checking how Annie is speaking\u{2026}" }
        guard voice.available else {
            return "Annie's dog isn't answering, so her voice can't be changed right now."
        }
        return "Speaking with \(Self.engine(voice.speak_via)), hearing with \(Self.engine(voice.hear_via))."
    }

    private static func engine(_ raw: String) -> String {
        switch raw.lowercased() {
        case "elevenlabs": return "ElevenLabs"
        case "deepgram": return "Deepgram"
        case "local": return "the computer's built-in voice"
        case "off": return "nothing (off)"
        default: return raw
        }
    }
}

/// A menu of device names. The first row is always the system default, which
/// the dog understands as an empty name.
private struct DevicePickerRow: View {
    let title: String
    let symbol: String
    let devices: [AudioDevice]
    let current: String?
    let enabled: Bool
    let choose: (String) -> Void

    private static let systemDefault = "System default"

    /// The dog reports "system default" when nothing is chosen.
    private var currentName: String {
        guard let current, !current.isEmpty, current.lowercased() != "system default" else { return Self.systemDefault }
        return current
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(Palette.slate)
                .frame(width: 22)
            Text(title)
            Spacer(minLength: 8)
            Menu {
                Button(Self.systemDefault) { choose("") }
                ForEach(devices) { device in
                    Button(device.name) { choose(device.name) }
                }
            } label: {
                HStack(spacing: 4) {
                    Text(currentName)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.caption2)
                }
                .font(.callout)
                .foregroundStyle(Palette.slate)
            }
            .disabled(!enabled)
        }
    }
}

/// One API key: a SecureField, Save, and the state of things in words.
private struct KeyRow: View {
    @EnvironmentObject private var state: AppState
    let title: String
    let account: Keychain.Account
    let dogHasKey: Bool
    let enabled: Bool

    @State private var typed = ""
    @State private var storedHere = false
    @State private var confirmRemove = false

    private var trimmed: String { typed.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Text(status)
                    .font(.caption)
                    .foregroundStyle(Palette.steel)
            }
            HStack(spacing: 8) {
                SecureField(storedHere ? "Saved on this phone" : "Paste key", text: $typed)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .onSubmit(save)
                Button("Save", action: save)
                    .buttonStyle(.borderedProminent)
                    .tint(Palette.slate)
                    .disabled(!enabled || trimmed.isEmpty)
            }
            HStack(spacing: 16) {
                // The dog keeps keys in memory, so it forgets them when it
                // restarts. Re-sending is offered, never done automatically.
                if storedHere && !dogHasKey {
                    Button("Send saved key to Annie", action: resend)
                        .disabled(!enabled)
                }
                if storedHere || dogHasKey {
                    // Removing clears the key on the dog as well, which stops
                    // her cloud voice mid-demo. Never a single tap.
                    Button("Remove") { confirmRemove = true }
                        .foregroundStyle(Palette.steel)
                        .disabled(!enabled)
                        .confirmationDialog("Remove the \(title)?", isPresented: $confirmRemove, titleVisibility: .visible) {
                            Button("Remove from this phone and from Annie", role: .destructive, action: remove)
                        } message: {
                            Text("Annie will fall back to the computer's built-in voice for this until a key is saved again.")
                        }
                }
            }
            .font(.caption.weight(.semibold))
            .tint(Palette.slate)
        }
        .onAppear { storedHere = Keychain.has(account) }
    }

    private var status: String {
        switch (dogHasKey, storedHere) {
        case (true, true): return "Annie has it \u{00b7} saved here"
        case (true, false): return "Annie has it"
        case (false, true): return "Saved here \u{00b7} not sent"
        case (false, false): return "Not set"
        }
    }

    private func update(with key: String) -> VoiceSettingsUpdate {
        switch account {
        case .elevenLabs: return VoiceSettingsUpdate(eleven_key: key)
        case .deepgram: return VoiceSettingsUpdate(deepgram_key: key)
        case .devBodyToken: return VoiceSettingsUpdate()
        }
    }

    private func save() {
        let key = trimmed
        guard !key.isEmpty else { return }   // never send an empty key: that clears the dog's
        let kept = Keychain.set(key, for: account)
        storedHere = Keychain.has(account)
        typed = ""
        Task {
            let sent = await state.updateVoice(update(with: key),
                                               saved: kept ? "\(title) saved on this phone and sent to Annie."
                                                           : "\(title) sent to Annie, but this phone couldn't save it. You'll need to paste it again next time.")
            if !sent, kept {
                state.voiceNote = .init(text: "\(title) is saved on this phone, but Annie didn't get it. Use \u{201c}Send saved key to Annie\u{201d} when she is back.", isError: true)
            }
        }
    }

    private func resend() {
        guard let key = Keychain.get(account), !key.isEmpty else {
            storedHere = false
            return
        }
        Task { await state.updateVoice(update(with: key), saved: "\(title) sent to Annie.") }
    }

    /// Explicitly clear the key here and on the dog (an empty key clears it there).
    private func remove() {
        Keychain.remove(account)
        storedHere = Keychain.has(account)
        typed = ""
        Task { await state.updateVoice(update(with: ""), saved: "\(title) removed from this phone and from Annie.") }
    }
}

// ---------------------------------------------------------------------------
// Advanced
// ---------------------------------------------------------------------------

/// Server address and token. Folded away: a family member never needs it once
/// the phone is set up, but the person running the demo does.
private struct AdvancedSettingsCard: View {
    @EnvironmentObject private var state: AppState
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) { open.toggle() }
            } label: {
                HStack {
                    Eyebrow(text: "Advanced")
                    Spacer()
                    Text(state.live ? "Connected" : "Not connected")
                        .font(.caption)
                        .foregroundStyle(Palette.steel)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Palette.steel)
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if open {
                ServerSettingsView()
            }
        }
        .annieCard()
    }
}
