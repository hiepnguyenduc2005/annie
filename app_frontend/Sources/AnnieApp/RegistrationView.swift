//
//  RegistrationView.swift
//  AnnieApp
//
//  First-run sign-in. The household's family members are fixed, so this is a
//  one-time "which of you is this phone" choice, not a name/password form.
//  Shown until a family member is signed in on this device.
//

import SwiftUI

struct RegistrationView: View {
    @EnvironmentObject private var profiles: ProfileState
    @State private var errorMessage: String?

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                AnnieMark(height: 52)
                    .foregroundStyle(Palette.slate)
                VStack(spacing: 8) {
                    Text("Welcome to Annie")
                        .font(.largeTitle.bold())
                        .multilineTextAlignment(.center)
                    Text("Keep in touch with Jeanine and see what Annie's noticed.")
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }

                VStack(spacing: 12) {
                    Text("Who's signing in?")
                        .font(.headline)
                    ForEach(FamilyMember.allCases) { member in
                        Button {
                            signIn(as: member)
                        } label: {
                            HStack {
                                Image(systemName: "person.crop.circle")
                                    .font(.title3)
                                Text(member.displayName)
                                    .font(.title3)
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .foregroundStyle(.secondary)
                            }
                            .padding(14)
                            .background(Palette.mist.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
                        }
                        .buttonStyle(.plain)
                    }
                }

                if let errorMessage {
                    Text(errorMessage).font(.caption).foregroundStyle(.red)
                }
            }
            .padding(24)
            .frame(maxWidth: 480)
            .frame(maxWidth: .infinity)
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 460)
        #endif
    }

    private func signIn(as member: FamilyMember) {
        do {
            try profiles.signIn(as: member)
        } catch {
            errorMessage = "This phone is already signed in."
        }
    }
}
