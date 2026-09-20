//
//  Views.swift
//  AnnieApp
//
//  Reminders (with today's routine), the activity feed, and the profile
//  card. Asking and messaging Annie are merged into one screen in
//  FamilyViews.swift.
//

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
                                Task { await state.send("tell Grandma to \(reminder.title.prefix(1).lowercased() + reminder.title.dropFirst())") }
                            } label: {
                                Label("Remind her", systemImage: "pawprint.fill")
                                    .font(.caption.weight(.semibold))
                                    .labelStyle(.titleAndIcon)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(Palette.slate)
                            .controlSize(.small)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
            .listStyle(.inset)

            Divider()

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
/// phone to a different family member.
struct AccountSectionView: View {
    @EnvironmentObject private var profiles: ProfileState
    @State private var confirmSignOut = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Account")
                .font(.annieHeading(22))
                .foregroundStyle(Palette.ink)
            HStack(spacing: 12) {
                Image(systemName: "person.crop.circle.fill")
                    .font(.largeTitle)
                    .foregroundStyle(Palette.slate)
                if let profile = profiles.profile {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(profile.member.displayName)
                            .font(.body.weight(.semibold))
                        Text("Signed in \u{00b7} \(profile.createdAt.formatted(date: .abbreviated, time: .omitted))")
                            .font(.caption).foregroundStyle(Palette.steel)
                    }
                }
                Spacer()
                Button("Sign out") { confirmSignOut = true }
                    .font(.callout.weight(.semibold))
                    .buttonStyle(.bordered)
                    .tint(Palette.slate)
                    .confirmationDialog("Sign out of Annie on this phone?", isPresented: $confirmSignOut) {
                        Button("Sign out", role: .destructive) { profiles.signOut() }
                    }
            }
            .annieCard()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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

