//
//  Theme.swift
//  AnnieApp
//
//  One palette for the whole app: black and white carry the interface, and
//  the blue-greys carry meaning (Annie's voice, actions, status).
//

import SwiftUI

enum Palette {
    /// #FFFFFF — page and card backgrounds.
    static let paper = Color.white
    /// #000000 — body text and the logo mark.
    static let ink = Color.black

    /// #B0BEC5 — the lightest accent: hairlines, resting fills.
    static let mist = Color(red: 0.690, green: 0.745, blue: 0.773)
    /// #78909C — secondary text and quiet progress lines.
    static let steel = Color(red: 0.471, green: 0.565, blue: 0.612)
    /// #455A64 — primary action, Annie's voice, emphasis.
    static let slate = Color(red: 0.271, green: 0.353, blue: 0.392)
}

/// The Annie mark. The asset lives in Assets.xcassets and is rendered as a
/// template, so it takes the surrounding foreground colour.
struct AnnieMark: View {
    var height: CGFloat = 28

    var body: some View {
        logoImage
            .renderingMode(.template)
            .resizable()
            .scaledToFit()
            .frame(height: height)
    }

    // SwiftPM puts resources in Bundle.module; the Xcode target uses the main
    // bundle. SWIFT_PACKAGE is only defined for the SwiftPM build.
    private var logoImage: Image {
        #if SWIFT_PACKAGE
        Image("AnnieLogo", bundle: .module)
        #else
        Image("AnnieLogo")
        #endif
    }
}
