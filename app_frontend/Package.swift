// swift-tools-version: 5.9
import PackageDescription

// The Annie companion front end: a SwiftUI app that talks to the Swift
// backend in backend/main.swift. Run it on the Mac from this directory with
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
    ]
)
