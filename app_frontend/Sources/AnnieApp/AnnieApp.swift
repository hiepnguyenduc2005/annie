//
//  AnnieApp.swift
//  AnnieApp
//
//  Entry point. The window shows a connection chip so it's always visible
//  whether we are connected to the backend.
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
                        .id(profile.user.id)
                        .task(id: profile.user.id) { await state.activate(profile) }
                } else {
                    RegistrationView()
                        .onAppear { state.deactivate() }
                }
            }
            .environmentObject(state)
            .environmentObject(profiles)
            // Tab bar, pickers and other system controls follow the palette
            // instead of the default iOS blue.
            .tint(Palette.slate)
            .onChange(of: scenePhase) { phase in
                if phase == .active { Task { await state.foreground() } }
                else { state.suspend() }
            }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                AnnieMark(height: 30)
                    .foregroundStyle(Palette.ink)
                Text("Annie")
                    .font(.title2.bold())
                    .foregroundStyle(Palette.ink)
                Spacer()
                Text(state.live ? "Connected" : "Offline")
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(
                        (state.live ? Palette.slate : Palette.steel).opacity(0.15),
                        in: Capsule()
                    )
                    .foregroundStyle(state.live ? Palette.slate : Palette.steel)
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
            }
            .padding(.top, 8)
        }
        #if os(macOS)
        .frame(minWidth: 620, minHeight: 560)
        #endif
    }
}
