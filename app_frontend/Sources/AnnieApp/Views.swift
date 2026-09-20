//
//  Views.swift
//  AnnieApp
//
//  Reminders (with today's routine), Ask Annie, the activity feed, and the
//  profile card. Sending messages to Annie lives in FamilyViews.swift.
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
// Ask Annie
// ---------------------------------------------------------------------------

struct AskView: View {
    @EnvironmentObject private var state: AppState
    @State private var question = ""

    private let suggestions = [
        "Where are her glasses?",
        "Did she take her medication?",
        "When is her walk?",
        "Did anyone visit today?",
    ]

    var body: some View {
        VStack(spacing: 16) {
            if let answer = state.answer {
                HStack(alignment: .top, spacing: 12) {
                    Text(answer)
                        .font(.title3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button {
                        state.speak(answer)
                    } label: {
                        Label("Read aloud", systemImage: "speaker.wave.2")
                    }
                    .help("Speak the answer with system speech synthesis")
                }
                .padding(16)
                .background(Palette.mist.opacity(0.30), in: RoundedRectangle(cornerRadius: 12))
            } else {
                Text("Ask Annie anything about Jeanine \u{2014} she'll answer from what she's seen today.")
                    .foregroundStyle(.secondary)
                    .frame(maxHeight: .infinity)
            }

            HStack {
                TextField("Ask Annie\u{2026}", text: $question)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(ask)
                Button("Ask", action: ask)
                    .buttonStyle(.borderedProminent)
                    .disabled(state.asking || question.trimmingCharacters(in: .whitespaces).isEmpty)
            }

            // Wraps onto extra rows on a narrow phone screen.
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 8)], spacing: 8) {
                ForEach(suggestions, id: \.self) { suggestion in
                    Button(suggestion) {
                        question = suggestion
                        ask()
                    }
                    .buttonStyle(.bordered)
                    .font(.callout)
                }
            }
        }
        .padding(20)
    }

    private func ask() {
        let q = question.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        Task { await state.ask(q) }
    }
}

// ---------------------------------------------------------------------------
// Activity feed
// ---------------------------------------------------------------------------

struct ActivityView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        List {
            ForEach(state.memory.reversed()) { fact in
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(fmtStamp(fact.timestamp)) \u{00b7} \(fact.room.replacingOccurrences(of: "_", with: " "))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(fact.text)
                }
                .padding(.vertical, 2)
            }
        }
        .listStyle(.inset)
        .overlay {
            if state.memory.isEmpty {
                Text("Nothing observed yet \u{2014} events appear here as Annie sees them.")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

struct ProfileView: View {
    @EnvironmentObject private var state: AppState
    @EnvironmentObject private var profiles: ProfileState

    private var meds: [Reminder] { state.reminders.filter { $0.title.lowercased().contains("medication") } }
    private var walk: Reminder? { state.reminders.first { $0.title.lowercased().contains("walk") } }

    var body: some View {
        ScrollView {
        VStack(spacing: 20) {
            HStack(spacing: 16) {
                AnnieMark(height: 40)
                    .foregroundStyle(Palette.slate)
                VStack(alignment: .leading, spacing: 2) {
                    Text(profiles.profile.map { "Hi, \($0.member.displayName)" } ?? "Hi")
                        .font(.title2.bold())
                    Text("Keeping an eye on Jeanine, together with Annie")
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            Text("Jeanine, today")
                .font(.headline)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 16) {
                StatCard(value: "\(meds.filter(\.done).count) of \(meds.count)", label: "medications taken")
                StatCard(value: walk.map { $0.done ? "done" : "due \(fmtClock($0.time))" } ?? "\u{2013}", label: "walk status")
                StatCard(value: "\(state.memory.count)", label: "things observed")
            }

            AccountSectionView()

            ServerSettingsView()

            Text("Annie is a staged assistance prototype, not a medical device or emergency response.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(20)
        }
    }
}

/// Who this phone is signed in as, with a way to sign out and hand the
/// phone to a different family member.
struct AccountSectionView: View {
    @EnvironmentObject private var profiles: ProfileState
    @State private var confirmSignOut = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Account")
                .font(.headline)
            if let profile = profiles.profile {
                HStack(spacing: 12) {
                    Image(systemName: "person.crop.circle.fill")
                        .font(.title2)
                        .foregroundStyle(Palette.slate)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(profile.member.displayName)
                        Text("Signed in \u{00b7} \(profile.createdAt.formatted(date: .abbreviated, time: .omitted))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                }
            }
            Button("Sign out", role: .destructive) { confirmSignOut = true }
                .font(.callout)
                .confirmationDialog("Sign out of Annie on this phone?", isPresented: $confirmSignOut) {
                    Button("Sign out", role: .destructive) { profiles.signOut() }
                }
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
                .font(.headline)
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
                    .disabled(connecting || lockedByEnvironment)
            }
            // Required off-device: the backend serves non-loopback clients
            // only when a token is configured and sent.
            SecureField("API token, if the backend has one set", text: $token)
                .textFieldStyle(.roundedBorder)
                .disabled(lockedByEnvironment)
                .onSubmit(connect)
            if lockedByEnvironment {
                Text("Set by the ANNIE_API_URL environment variable.")
                    .font(.caption).foregroundStyle(.secondary)
            } else if invalid {
                Text("Enter an address like 192.168.1.20:8000 or http://my-mac.local:8000.")
                    .font(.caption).foregroundStyle(.red)
            } else {
                Text(state.live ? "Connected to \(state.serverURL.absoluteString)"
                                : "Not connected to \(state.serverURL.absoluteString) \u{2014} showing demo data.")
                    .font(.caption).foregroundStyle(.secondary)
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

struct StatCard: View {
    let value: String
    let label: String

    var body: some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.title.bold())
                .foregroundStyle(Palette.slate)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.horizontal, 4)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 14)
        .background(Palette.mist.opacity(0.22), in: RoundedRectangle(cornerRadius: 12))
    }
}
