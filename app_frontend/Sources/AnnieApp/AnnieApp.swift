//
//  AnnieApp.swift
//  AnnieApp
//
//  Entry point. The window shows a connection chip so it's always visible
//  whether we're talking to the live backend or running on bundled demo data.
//

import SwiftUI
#if os(macOS)
import AppKit
#endif

@main
struct AnnieApp: App {
    @StateObject private var state = AppState()
    @StateObject private var profiles = ProfileState()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        #if os(macOS)
        // Launched via `swift run` there's no .app bundle, so macOS starts the
        // process as background-only (no window, no Dock icon). Promote it.
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
        #endif
    }

    var body: some Scene {
        WindowGroup("Annie") {
            Group {
                if let profile = profiles.profile {
                    ContentView()
                        .task {
                            state.authorID = profile.member.rawValue
                            await state.load()
                        }
                } else {
                    RegistrationView()
                }
            }
            .environmentObject(state)
            .environmentObject(profiles)
            // Tab bar, pickers and other system controls follow the palette
            // instead of the default iOS blue.
            .tint(Palette.slate)
            .onChange(of: scenePhase) { phase in
                if phase == .active { Task { await state.reconnectIfOffline() } }
            }
        }
    }
}

/// Shown wherever the dog's state is: what is answering is the simulated dog,
/// not the real one in the real home.
struct SimulatorPill: View {
    var body: some View {
        Label("Simulator", systemImage: "cube.transparent")
            .font(.caption2.weight(.bold))
            .labelStyle(.titleAndIcon)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .foregroundStyle(Palette.slate)
            .overlay(Capsule().stroke(Palette.slate, lineWidth: 1))
            .accessibilityLabel("Simulated dog")
    }
}

struct ContentView: View {
    @EnvironmentObject private var state: AppState
    @EnvironmentObject private var profiles: ProfileState

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                AnnieMark(height: 30)
                    .foregroundStyle(Palette.ink)
                Text("Annie")
                    .font(.title2.bold())
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .fixedSize()
                Spacer(minLength: 4)
                if state.live, state.dog?.isSimulated == true {
                    // The simulated-dog mark takes the status chip's place: one
                    // word on where the answer is coming from, no jargon.
                    SimulatorPill()
                } else {
                    Text(state.live ? "Live" : "Offline \u{00b7} demo data")
                        .font(.caption.weight(.semibold))
                        .lineLimit(1)
                        .fixedSize()
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(
                            (state.live ? Palette.slate : Palette.steel).opacity(0.15),
                            in: Capsule()
                        )
                        .foregroundStyle(state.live ? Palette.slate : Palette.steel)
                }
                if profiles.picture != nil {
                    ProfileAvatar(image: profiles.picture, size: 30)
                        .accessibilityLabel(profiles.profile.map { "Signed in as \($0.member.displayName)" } ?? "Profile picture")
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider()

            TabView {
                RemindersView()
                    .tabItem { Label("Reminders", systemImage: "checklist") }
                AskAnnieView()
                    .tabItem { Label("Ask Annie", systemImage: "pawprint.fill") }
                ActivityView()
                    .tabItem { Label("History", systemImage: "clock.arrow.circlepath") }
                ProfileView()
                    .tabItem { Label("Profile", systemImage: "person.crop.circle") }
                if AppConfiguration.devFeedAvailable {  // debug feed from the dog process; only with a Server set
                    DevFeedView()
                        .tabItem { Label("Dev", systemImage: "flask") }
                }
            }
            .padding(.top, 8)
        }
        #if os(macOS)
        .frame(minWidth: 620, minHeight: 560)
        #endif
    }
}
