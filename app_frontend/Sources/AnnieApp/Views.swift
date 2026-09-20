import SwiftUI

struct SectionError: View {
    let text: String?
    var body: some View {
        if let text {
            Text(text).font(.callout).foregroundStyle(.red)
                .frame(maxWidth: .infinity, alignment: .leading).padding(12)
        }
    }
}

struct RemindersView: View {
    @EnvironmentObject private var state: AppState
    @State private var showAdd = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(state.residentName)'s reminders").font(.headline)
                    Text("Shared with the family · \(state.timezone)").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(12)
            SectionError(text: state.reminderError)
            List {
                ForEach(state.reminders) { reminder in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: reminder.done == true ? "checkmark.circle.fill" : "circle")
                            .font(.title2).foregroundStyle(Palette.slate)
                            .accessibilityLabel(reminder.done == true ? "Reported complete today" : "Not reported complete today")
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Text(fmtClock(reminder.daily_time)).font(.subheadline.weight(.semibold)).monospacedDigit()
                                Text(reminder.description).strikethrough(reminder.done == true)
                            }
                            if let note = reminder.latest_note {
                                Text(note.description).font(.callout).foregroundStyle(.secondary)
                                Text("\(note.source == "seed" ? "Demo example · " : "")\(displayTimestamp(note.timestamp, timezone: state.timezone))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                    }.padding(.vertical, 6)
                }
                if state.reminders.isEmpty && !state.loading {
                    Text("No reminders yet. Add one for the family to see.").foregroundStyle(.secondary)
                }
            }
            .listStyle(.inset)
            .refreshable { await state.refreshReminders() }
            HStack {
                Button { showAdd = true } label: { Label("Add reminder", systemImage: "plus.circle.fill") }
                Spacer()
                Text("\(state.reminders.filter { $0.done == true }.count) of \(state.reminders.count) completed today")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(12)
        }
        .sheet(isPresented: $showAdd) { AddReminderView() }
    }
}

struct AddReminderView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var time = Date()
    @State private var description = ""

    var body: some View {
        VStack(spacing: 16) {
            Text("New shared reminder").font(.title2.bold())
            DatePicker("Daily time", selection: $time, displayedComponents: .hourAndMinute)
                .environment(\.timeZone, TimeZone(identifier: state.timezone) ?? .current)
            Text(state.timezone).font(.caption).foregroundStyle(.secondary)
            TextField("What should Annie remind \(state.residentName) about?", text: $description)
                .textFieldStyle(.roundedBorder)
                .disabled(state.adding)
            Text("Everyone in \(state.residentName)'s family will see this reminder.")
                .font(.caption).foregroundStyle(.secondary)
            SectionError(text: state.addError)
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).disabled(state.adding)
                Button(state.adding ? "Saving…" : "Add") {
                    let formatter = DateFormatter()
                    formatter.locale = Locale(identifier: "en_US_POSIX")
                    formatter.timeZone = TimeZone(identifier: state.timezone)
                    formatter.dateFormat = "HH:mm"
                    let dailyTime = formatter.string(from: time)
                    Task {
                        if await state.add(time: dailyTime, description: description) { dismiss() }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(state.adding || description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || description.count > 4000)
                .keyboardShortcut(.defaultAction)
            }
        }.padding(24).frame(maxWidth: 420)
        .interactiveDismissDisabled(state.adding)
    }
}

struct ActivityView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            SectionError(text: state.historyError)
            List {
                ForEach(state.history) { item in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: item.is_emergency ? "exclamationmark.triangle.fill" : item.kind == "note" ? "checklist" : "bell")
                            .foregroundStyle(item.is_emergency ? Color.red : Palette.slate)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.is_emergency ? "Urgent notification" : item.kind == "note" ? "Reminder report" : "Update")
                                .font(.subheadline.weight(.semibold))
                            Text(item.description)
                            Text("\(item.source == "seed" ? "Demo example · " : "")\(displayTimestamp(item.timestamp, timezone: state.timezone))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }.padding(.vertical, 6)
                }
                if state.history.isEmpty && !state.loadingHistory {
                    Text("Reminder reports and notifications will appear here.").foregroundStyle(.secondary)
                }
                if state.historyCursor != nil {
                    Button(state.loadingHistory ? "Loading…" : "Load earlier activity") {
                        Task { await state.refreshHistory(more: true) }
                    }.disabled(state.loadingHistory)
                }
            }
            .listStyle(.inset)
            .refreshable { await state.refreshHistory() }
        }
    }
}

struct ProfileView: View {
    @EnvironmentObject private var profiles: ProfileState
    @EnvironmentObject private var state: AppState
    @State private var confirmSignOut = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text("Account").font(.headline)
                if let profile = profiles.profile {
                    Label(profile.user.name, systemImage: "person.crop.circle.fill").font(.title2)
                    Text("\(profile.resident.name)'s family").foregroundStyle(.secondary)
                    Text("Resident timezone: \(state.timezone)").font(.caption).foregroundStyle(.secondary)
                }
                Button("Sign out", role: .destructive) { confirmSignOut = true }
                    .confirmationDialog("Sign out of Annie on this phone?", isPresented: $confirmSignOut) {
                        Button("Sign out", role: .destructive) { state.deactivate(); profiles.signOut() }
                    }
                Divider()
                Text("Connection").font(.headline)
                Text(state.live ? (state.socketConnected ? "Connected · live updates" : "Connected · refreshing periodically") : "Not connected")
                if let error = state.connectionError {
                    Text(error).font(.callout).foregroundStyle(.red)
                    Text(state.serverURL.absoluteString).font(.caption).foregroundStyle(.secondary)
                }
                Button(state.loading ? "Connecting…" : "Connect") { Task { await state.foreground() } }
                    .buttonStyle(.borderedProminent).disabled(state.loading)
                Text("Annie is a staged assistance prototype, not a medical device or emergency response.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
