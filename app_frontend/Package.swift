// swift-tools-version: 5.9
import PackageDescription

// The Annie companion front end: a SwiftUI app that talks to the Swift
// v2 backend in app_backend. Run it on the Mac from this directory with
// `swift run`. The same Sources/AnnieApp folder also builds the iPhone app via
// Annie.xcodeproj (see README.swift).
let package = Package(
    name: "AnnieApp",
    platforms: [.macOS(.v13), .iOS(.v16)],
    targets: [
        .executableTarget(
            name: "AnnieApp",
            path: "Sources/AnnieApp",
            resources: [.process("Assets.xcassets")]
        ),
        .testTarget(name: "AnnieAppTests", dependencies: ["AnnieApp"], path: "Tests/AnnieAppTests"),
    ]
)
