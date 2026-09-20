//
//  RegistrationView.swift
//  AnnieApp
//
//  First-run profile registration. Choose app user or dog user, then fill in
//  a name. An app user also names the dog, so both profiles are created in
//  one step. Shown until a profile exists on this device.
//

import SwiftUI

struct RegistrationView: View {
    @EnvironmentObject private var profiles: ProfileState
    @State private var kind: ProfileKind?
    @State private var name = ""
    @State private var dogName = "Annie"
    @State private var errorMessage: String?

    private var canCreate: Bool {
        ProfileRegistration.cleaned(name) != nil
            && (kind != .appUser || ProfileRegistration.cleaned(dogName) != nil)
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                Image(systemName: "pawprint.fill")
                    .font(.system(size: 44))
                    .foregroundStyle(Palette.slate)
                Text("Welcome to Annie")
                    .font(.largeTitle.bold())
                    .multilineTextAlignment(.center)

                if let kind {
                    form(for: kind)
                } else {
                    chooser
                }
            }
            .padding(24)
            .frame(maxWidth: 480)
            .frame(maxWidth: .infinity)
        }
        #if os(macOS)
        .frame(minWidth: 620, minHeight: 460)
        #endif
    }

    // MARK: Step 1 — who is registering

    private var chooser: some View {
        VStack(spacing: 12) {
            Text("Who is setting up this profile?")
                .foregroundStyle(.secondary)
            roleButton(.appUser, title: "App user",
                       detail: "You use this app. Annie the dog's profile is created with yours.")
            roleButton(.dogUser, title: "Dog user",
                       detail: "Create Annie the dog's own profile.")
        }
    }

    private func roleButton(_ choice: ProfileKind, title: String, detail: String) -> some View {
        Button {
            kind = choice
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Palette.mist.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
    }

    // MARK: Step 2 — details

    private func form(for kind: ProfileKind) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(kind == .appUser ? "Create your profile" : "Create the dog's profile")
                .font(.title3.bold())

            field(kind == .appUser ? "Your name" : "Dog's name", text: $name)

            if kind == .appUser {
                Text("Annie's profile")
                    .font(.headline)
                    .padding(.top, 4)
                Text("Created together with yours.")
                    .font(.caption).foregroundStyle(.secondary)
                field("Dog's name", text: $dogName)
            }

            if let errorMessage {
                Text(errorMessage).font(.caption).foregroundStyle(.red)
            }

            HStack {
                Button("Back") {
                    self.kind = nil
                    errorMessage = nil
                }
                Spacer()
                Button(kind == .appUser ? "Create profiles" : "Create profile") { create(kind) }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canCreate)
            }
            .padding(.top, 4)
        }
    }

    private func field(_ title: String, text: Binding<String>) -> some View {
        TextField(title, text: text)
            .textFieldStyle(.roundedBorder)
            #if os(iOS)
            .textInputAutocapitalization(.words)
            #endif
            .onSubmit { if canCreate, let kind { create(kind) } }
    }

    private func create(_ kind: ProfileKind) {
        do {
            switch kind {
            case .appUser: try profiles.registerAppUser(name: name, dogName: dogName)
            case .dogUser: try profiles.registerDogUser(name: name)
            }
        } catch ProfileError.invalidName {
            errorMessage = "Enter a name up to \(ProfileRegistration.maxNameLength) characters."
        } catch {
            errorMessage = "Profiles are already set up on this device."
        }
    }
}
