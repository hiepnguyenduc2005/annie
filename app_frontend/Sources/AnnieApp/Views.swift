//
//  Views.swift
//  AnnieApp
//
//  Reminders (with today's routine), the activity feed, and the profile
//  card. Asking and messaging Annie are merged into one screen in
//  FamilyViews.swift.
//

import PhotosUI
import SwiftUI

// ---------------------------------------------------------------------------
// Reminders
// ---------------------------------------------------------------------------

struct RemindersView: View {
    @EnvironmentObject private var state: AppState
    @State private var showAdd = false

    private var doneCount: Int { state.reminders.filter(\.done).count }

    var body: some View {
        VStack(spacing: 0) {
            List {
                ForEach(state.reminders) { reminder in
                    VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 12) {
                        Button {
                            Task { await state.toggle(reminder) }
                        } label: {
                            Image(systemName: reminder.done ? "checkmark.circle.fill" : "circle")
                                .font(.title2)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(reminder.done ? Palette.slate : Palette.steel)
                        .help(reminder.done ? "Mark as not done" : "Mark as done")

                        Text(fmtClock(reminder.time))
                            .monospacedDigit()
                            .frame(width: 84, alignment: .leading)

                        Text(reminder.title)
                            .strikethrough(reminder.done)
                            .foregroundStyle(reminder.done ? .secondary : .primary)
                        Spacer()
                        if !reminder.done {
                            // The reminder goes through the same path as a family message: Annie finds her,
                            // says it, listens for the reply; the run shows under Ask Annie.
                            Button {
                                Task { await state.remind(reminder) }
                            } label: {
                                // One line, always: the title wraps instead of the button.
                                HStack(spacing: 5) {
                                    Image(systemName: "pawprint.fill")
                                    Text(state.reminderInFlight == reminder.id ? "Sending…" : "Remind")
                                }
                                .font(.caption.weight(.semibold))
                                .lineLimit(1)
                                .fixedSize(horizontal: true, vertical: false)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(Palette.slate)
                            .controlSize(.small)
                            .fixedSize(horizontal: true, vertical: false)
                            .accessibilityLabel("Remind her: \(reminder.title)")
                            .disabled(state.sending || state.pausing || state.reminderRun(for: reminder).map { !$0.finished } == true)
                        }
                    }
                    .padding(.vertical, 2)
                    if let run = state.reminderRun(for: reminder) {
                        HStack(alignment: .top, spacing: 8) {
                            if !run.finished { ProgressView().controlSize(.small) }
                            VStack(alignment: .leading, spacing: 3) {
                                Text(run.statusLabel).font(.caption.weight(.semibold))
                                if let event = run.events.last, run.status != "cancelled" {
                                    Text(event.summary).font(.caption)
                                }
                            }
                            Spacer()
                            if !run.finished {
                                Button("Pause") { Task { await state.pauseTasks() } }
                                    .buttonStyle(.bordered)
                                    .disabled(state.pausing)
                            }
                        }
                        .foregroundStyle(["failed", "unreachable", "unknown"].contains(run.status) ? Palette.alert : Palette.slate)
                        .padding(.bottom, 5)
                    }
                    }
                }
            }
            .listStyle(.inset)

            Divider()

            if let note = state.reminderNote ?? state.commandNote {
                Text(note.text)
                    .font(.footnote)
                    .foregroundStyle(note.isError ? Palette.alert : Palette.slate)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
            }

            HStack {
                Button {
                    showAdd = true
                } label: {
                    Label("Add reminder", systemImage: "plus.circle.fill")
                }
                Spacer()
                Text("\(doneCount) of \(state.reminders.count) done")
                    .foregroundStyle(.secondary)
            }
            .padding(12)
        }
        .sheet(isPresented: $showAdd) {
            AddReminderView()
        }
    }
}

struct AddReminderView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var time = Date()
    @State private var title = ""

    private static let hhmm: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter
    }()

    var body: some View {
        VStack(spacing: 16) {
            Text("New reminder")
                .font(.title2.bold())
            DatePicker("Time", selection: $time, displayedComponents: .hourAndMinute)
            TextField("What should Annie remind you about?", text: $title)
                .textFieldStyle(.roundedBorder)
                .onSubmit(add)
            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add", action: add)
                    .buttonStyle(.borderedProminent)
                    .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(maxWidth: 360)
    }

    private func add() {
        let trimmed = title.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        let hhmm = Self.hhmm.string(from: time)
        Task {
            await state.add(time: hhmm, title: trimmed)
            dismiss()
        }
    }
}

// ---------------------------------------------------------------------------
// Activity feed
// ---------------------------------------------------------------------------

/// History: what Annie has seen, newest first. Facts the dog is reporting
/// right now (negative ids, merged in by the backend from her live memory)
/// sit on top under "Live from Annie"; the seeded and family-added ones sit
/// under "Earlier".
struct ActivityView: View {
    @EnvironmentObject private var state: AppState

    // ISO 8601 strings of one fixed format sort correctly as text.
    private var liveFacts: [MemoryFact] {
        state.memory.filter(\.isLive).sorted { $0.timestamp > $1.timestamp }
    }
    private var earlierFacts: [MemoryFact] {
        state.memory.filter { !$0.isLive }.sorted { $0.timestamp > $1.timestamp }
    }

    var body: some View {
        List {
            if !liveFacts.isEmpty {
                Section {
                    ForEach(liveFacts) { FactRow(fact: $0) }
                } header: {
                    HStack(spacing: 8) {
                        Circle().fill(Palette.liveDot).frame(width: 9, height: 9)
                        Eyebrow(text: "Live from Annie")
                    }
                }
            }
            if !earlierFacts.isEmpty {
                Section {
                    ForEach(earlierFacts) { FactRow(fact: $0) }
                } header: {
                    Eyebrow(text: "Earlier")
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Palette.cream)
        .refreshable { await state.refreshHistory() }
        .overlay {
            if state.memory.isEmpty {
                Text("Nothing observed yet. Events appear here as Annie sees them.")
                    .foregroundStyle(Palette.steel)
                    .multilineTextAlignment(.center)
                    .padding(32)
            }
        }
    }
}

private struct FactRow: View {
    let fact: MemoryFact

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(fmtDayStamp(fact.timestamp)) \u{00b7} \(fact.room.replacingOccurrences(of: "_", with: " "))")
                .font(.caption)
                .foregroundStyle(Palette.steel)
            Text(fact.text)
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
        .annieCard(padding: 12)
        .listRowSeparator(.hidden)
        .listRowBackground(Color.clear)
        .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
    }
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

struct ProfileView: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                AccountSectionView()

                PeopleSectionView()

                SettingsSectionView()

                Text("Annie is a staged assistance prototype, not a medical device or emergency response.")
                    .font(.caption)
                    .foregroundStyle(Palette.steel)
            }
            .padding(16)
        }
        #if os(iOS)
        .scrollDismissesKeyboard(.interactively)
        #endif
        .background(Palette.cream)
    }
}

/// Who this phone is signed in as, with a way to sign out and hand the
/// phone to a different family member. The picture is chosen here, kept on
/// this phone only, and shown again in the header.
struct AccountSectionView: View {
    @EnvironmentObject private var profiles: ProfileState
    @State private var confirmSignOut = false
    @State private var pick: PhotosPickerItem?
    @State private var pictureProblem: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Account")
                .font(.annieHeading(22))
                .foregroundStyle(Palette.ink)
            HStack(spacing: 12) {
                PhotosPicker(selection: $pick, matching: .images) {
                    ProfileAvatar(image: profiles.picture, size: 52)
                        .overlay(alignment: .bottomTrailing) {
                            Image(systemName: "camera.fill")
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(.white)
                                .frame(width: 20, height: 20)
                                .background(Palette.slate, in: Circle())
                                .overlay(Circle().stroke(Palette.paper, lineWidth: 2))
                                .offset(x: 3, y: 3)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(profiles.picture == nil ? "Add a profile picture" : "Change profile picture")
                .contextMenu {
                    if profiles.picture != nil {
                        Button(role: .destructive) {
                            profiles.removePicture()
                        } label: {
                            Label("Remove picture", systemImage: "trash")
                        }
                    }
                }

                if let profile = profiles.profile {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(profile.member.displayName)
                            .font(.body.weight(.semibold))
                        Text("Signed in \u{00b7} \(profile.createdAt.formatted(date: .abbreviated, time: .omitted))")
                            .font(.caption).foregroundStyle(Palette.steel)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                    }
                }
                Spacer(minLength: 8)
                Button("Sign out") { confirmSignOut = true }
                    .font(.callout.weight(.semibold))
                    .buttonStyle(.bordered)
                    .tint(Palette.slate)
                    .confirmationDialog("Sign out of Annie on this phone?", isPresented: $confirmSignOut) {
                        Button("Sign out", role: .destructive) { profiles.signOut() }
                    }
            }
            .annieCard()

            if let pictureProblem {
                Text(pictureProblem)
                    .font(.footnote)
                    .foregroundStyle(Palette.alert)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: pick) { item in
            guard let item else { return }
            Task { @MainActor in
                let data = try? await item.loadTransferable(type: Data.self)
                let kept = data.map { profiles.setPicture(from: $0) } ?? false
                pictureProblem = kept ? nil : "That picture couldn't be used. Try a different one."
                pick = nil
            }
        }
    }
}

/// Where the Annie API lives. On a phone this must be the Mac's LAN address
/// (e.g. `192.168.1.20:8000`) — `127.0.0.1` there is the phone itself.
struct ServerSettingsView: View {
    @EnvironmentObject private var state: AppState
    @State private var text = ""
    @State private var token = ""
    @State private var invalid = false
    @State private var connecting = false

    private var lockedByEnvironment: Bool { AppConfiguration.environmentOverride != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Server")
                .font(.subheadline.weight(.semibold))
            HStack {
                TextField("Mac address, e.g. 192.168.1.20:8000", text: $text)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif
                    .onSubmit(connect)
                    .disabled(lockedByEnvironment)
                Button(connecting ? "Connecting\u{2026}" : "Connect", action: connect)
                    .buttonStyle(.borderedProminent)
                    .tint(Palette.slate)
                    .disabled(connecting || lockedByEnvironment)
            }
            // Required off-device: the backend serves non-loopback clients
            // only when a token is configured and sent.
            SecureField("API token, if the backend has one set", text: $token)
                .textFieldStyle(.roundedBorder)
                .disabled(lockedByEnvironment)
                .onSubmit(connect)
            if lockedByEnvironment {
                Text("Set by the ANNIE_API_URL environment variable: \(state.serverURL.absoluteString)")
                    .font(.caption).foregroundStyle(Palette.steel)
            } else if invalid {
                Text("Enter an address like 192.168.1.20:8000 or http://my-mac.local:8000.")
                    .font(.caption).foregroundStyle(Palette.alert)
            } else {
                Text(state.live ? "Connected to \(state.serverURL.absoluteString)"
                                : "Not connected to \(state.serverURL.absoluteString). Showing demo data.")
                    .font(.caption).foregroundStyle(Palette.steel)
            }
        }
        .onAppear {
            text = lockedByEnvironment ? "" : state.serverURL.absoluteString
            token = AppConfiguration.apiToken
        }
    }

    private func connect() {
        connecting = true
        Task {
            invalid = !(await state.setServer(text, token: token))
            connecting = false
        }
    }
}
