import SwiftUI

struct RegistrationView: View {
    @EnvironmentObject private var profiles: ProfileState

    var body: some View {
        ScrollView {
            VStack(spacing: 22) {
                AnnieMark(height: 52).foregroundStyle(Palette.slate)
                Text("Welcome to Annie").font(.largeTitle.bold())
                Text("Choose your family profile to see your shared reminders and talk to Annie.")
                    .multilineTextAlignment(.center).foregroundStyle(.secondary)
                if profiles.loading { ProgressView("Loading family members…") }
                ForEach(profiles.users) { user in
                    Button { profiles.signIn(user) } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "person.crop.circle").font(.title2)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(user.name).font(.title3)
                                if let resident = profiles.residents.first(where: { $0.id == user.dog_user_id }) {
                                    Text("\(resident.name)'s family").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                        }
                        .padding(16)
                        .background(Palette.mist.opacity(0.2), in: RoundedRectangle(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                }
                if let error = profiles.error {
                    Text(error).foregroundStyle(.red).font(.callout)
                } else if profiles.users.isEmpty && !profiles.loading {
                    Text("No family profiles are available yet.").foregroundStyle(.secondary)
                }
                Button("Refresh family members") { Task { await profiles.loadMembers() } }
                    .disabled(profiles.loading)
            }
            .padding(24).frame(maxWidth: 480).frame(maxWidth: .infinity)
        }
        .task { await profiles.loadMembers() }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 460)
        #endif
    }
}
