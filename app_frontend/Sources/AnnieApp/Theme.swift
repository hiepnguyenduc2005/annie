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

    /// #F4F6F7 — the page: a cool near-white so white cards separate from it.
    static let cream = Color(red: 0.957, green: 0.965, blue: 0.969)
    /// #CFD8DC — card borders and dividers.
    static let line = Color(red: 0.812, green: 0.847, blue: 0.863)
    /// #ECEFF1 — resting fill for chips and quiet buttons.
    static let soft = Color(red: 0.925, green: 0.937, blue: 0.945)
    /// #A33A2E — urgency only (Stop, failures). Deliberately outside the
    /// blue-greys so it stays legible in a safety product.
    static let alert = Color(red: 0.639, green: 0.227, blue: 0.180)
    /// #3E7D5A — one quiet green, used only for the "this is live from the
    /// dog right now" dot. Never for buttons or text.
    static let liveDot = Color(red: 0.243, green: 0.490, blue: 0.353)
}

extension Font {
    /// Georgia carries headings, matching `frontend/styles.css`; system sans
    /// carries everything else.
    static func annieHeading(_ size: CGFloat = 22) -> Font {
        .custom("Georgia", size: size)
    }
}

/// A white card on the cream page, with the hairline border the web UI uses.
struct CardBackground: ViewModifier {
    var padding: CGFloat = 16

    func body(content: Content) -> some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.paper, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(Palette.line, lineWidth: 1)
            )
    }
}

extension View {
    func annieCard(padding: CGFloat = 16) -> some View {
        modifier(CardBackground(padding: padding))
    }
}

/// Small uppercase label above a section, like `.eyebrow` on the web.
struct Eyebrow: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .bold))
            .tracking(1.6)
            .foregroundStyle(Palette.steel)
    }
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
