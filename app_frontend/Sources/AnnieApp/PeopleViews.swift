//
//  PeopleViews.swift
//  AnnieApp
//
//  Profile > People Annie knows: who she can greet by name, a sheet to add
//  someone from a few photos, and swipe to forget them. Also the small shared
//  pieces for pictures: downscaling to JPEG and the round avatar.
//
//  Photos picked here are downscaled on the phone (640 px, JPEG 0.8), sent
//  once through POST /api/people so the dog can compute a face embedding, and
//  then dropped. Nothing in this file writes them to disk.
//

import ImageIO
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

// ---------------------------------------------------------------------------
// Pictures
// ---------------------------------------------------------------------------

/// ImageIO rather than UIImage, so the same code builds for the Mac target.
enum Photo {
    /// Re-encode as a JPEG no larger than `maxPixel` on its longest side, with
    /// the camera's rotation applied. Never upscales. nil if it isn't an image.
    static func jpeg(from data: Data, maxPixel: Int, quality: Double) -> Data? {
        guard let image = cgImage(from: data, maxPixel: maxPixel) else { return nil }
        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(output, UTType.jpeg.identifier as CFString, 1, nil) else {
            return nil
        }
        let options = [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary
        CGImageDestinationAddImage(destination, image, options)
        return CGImageDestinationFinalize(destination) ? output as Data : nil
    }

    static func cgImage(from data: Data, maxPixel: Int) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }
}

/// A round picture, or the person glyph when there isn't one.
struct ProfileAvatar: View {
    let image: CGImage?
    var size: CGFloat = 44

    var body: some View {
        Group {
            if let image {
                Image(decorative: image, scale: 1)
                    .resizable()
                    .scaledToFill()
            } else {
                Image(systemName: "person.crop.circle.fill")
                    .resizable()
                    .scaledToFit()
                    .foregroundStyle(Palette.slate)
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .overlay(Circle().stroke(Palette.line, lineWidth: image == nil ? 0 : 1))
    }
}

// ---------------------------------------------------------------------------
// Shirt colours
// ---------------------------------------------------------------------------

/// The colours the dog's shirt-colour identity understands. These are literal
/// swatches of clothing, so they sit outside the interface palette on purpose.
enum ShirtColour: String, CaseIterable, Identifiable {
    case red, blue, green, black, white, yellow

    var id: String { rawValue }
    var label: String { rawValue.capitalized }

    var color: Color {
        switch self {
        case .red: return Color(red: 0.75, green: 0.22, blue: 0.17)
        case .blue: return Color(red: 0.18, green: 0.43, blue: 0.69)
        case .green: return Color(red: 0.24, green: 0.49, blue: 0.35)
        case .black: return .black
        case .white: return .white
        case .yellow: return Color(red: 0.91, green: 0.74, blue: 0.20)
        }
    }
}

private struct ShirtSwatch: View {
    let colour: ShirtColour?
    var size: CGFloat = 16

    var body: some View {
        ZStack {
            Circle().fill(colour?.color ?? Palette.soft)
            if colour == nil {
                Image(systemName: "line.diagonal")
                    .font(.system(size: size * 0.7, weight: .semibold))
                    .foregroundStyle(Palette.steel)
            }
        }
        .frame(width: size, height: size)
        .overlay(Circle().stroke(Palette.line, lineWidth: 1))
    }
}

// ---------------------------------------------------------------------------
// The section on the Profile tab
// ---------------------------------------------------------------------------

struct PeopleSectionView: View {
    @EnvironmentObject private var state: AppState
    @State private var showAdd = false

    private static let rowHeight: CGFloat = 60

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("People Annie knows")
                    .font(.annieHeading(22))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                Spacer(minLength: 8)
                Button {
                    showAdd = true
                } label: {
                    Label("Add person", systemImage: "plus")
                        .font(.callout.weight(.semibold))
                        .labelStyle(.titleAndIcon)
                        .lineLimit(1)
                        .fixedSize()
                }
                .buttonStyle(.bordered)
                .tint(Palette.slate)
                .disabled(!state.live)
            }

            Group {
                if let people = state.people, !people.isEmpty {
                    // A List, so rows get the system swipe-to-delete. It is
                    // sized to its rows and does not scroll; the page does.
                    List {
                        ForEach(people) { person in
                            PersonRow(person: person)
                                .frame(height: Self.rowHeight)
                                .listRowInsets(EdgeInsets(top: 0, leading: 14, bottom: 0, trailing: 14))
                                .listRowBackground(Palette.paper)
                                .listRowSeparatorTint(Palette.line)
                                // The card's own border closes the list; a last hairline doubles it.
                                .listRowSeparator(person.id == people.last?.id ? .hidden : .automatic, edges: .bottom)
                                .listRowSeparator(person.id == people.first?.id ? .hidden : .automatic, edges: .top)
                                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                    Button(role: .destructive) {
                                        Task { await state.forget(person) }
                                    } label: {
                                        Label("Forget", systemImage: "trash")
                                    }
                                    .tint(Palette.alert)
                                }
                                .contextMenu {
                                    Button(role: .destructive) {
                                        Task { await state.forget(person) }
                                    } label: {
                                        Label("Forget \(person.name)", systemImage: "trash")
                                    }
                                }
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .scrollDisabled(true)
                    .environment(\.defaultMinListRowHeight, Self.rowHeight)
                    .frame(height: Self.rowHeight * CGFloat(people.count))
                    .background(Palette.paper)
                    .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .stroke(Palette.line, lineWidth: 1)
                    )
                } else {
                    Text(emptyText)
                        .font(.callout)
                        .foregroundStyle(Palette.steel)
                        .fixedSize(horizontal: false, vertical: true)
                        .annieCard()
                }
            }

            if state.live, state.people?.isEmpty == false {
                Text(state.peopleAvailable ? "Swipe a name left to have Annie forget them."
                                           : "Annie's dog isn't answering. This is who she knew when she last did.")
                    .font(.caption)
                    .foregroundStyle(Palette.steel)
            }

            if let note = state.peopleNote {
                Text(note.text)
                    .font(.footnote)
                    .foregroundStyle(note.isError ? Palette.alert : Palette.slate)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task { await state.loadPeople() }
        // The list lives on the dog: ask again whenever she comes or goes.
        .onChange(of: state.dog?.available) { _ in
            Task { await state.loadPeople() }
        }
        .onChange(of: state.live) { _ in
            Task { await state.loadPeople() }
        }
        .sheet(isPresented: $showAdd) {
            AddPersonSheet()
        }
    }

    private var emptyText: String {
        guard state.live else { return "Not connected to Annie. Set the server under Settings, Advanced." }
        guard state.people != nil else { return "Checking who Annie knows\u{2026}" }
        guard state.peopleAvailable else {
            return "Annie's dog isn't answering, so she can't say who she knows right now."
        }
        return "Annie doesn't know anyone by name yet. Add someone with a few clear photos and she will greet them by name."
    }
}

private struct PersonRow: View {
    let person: KnownPerson

    private var initial: String { String(person.name.prefix(1)).uppercased() }

    private var faces: String {
        switch person.faces {
        case 0: return "No face yet"
        case 1: return "1 face sample"
        default: return "\(person.faces) face samples"
        }
    }

    var body: some View {
        HStack(spacing: 12) {
            Text(initial)
                .font(.annieHeading(18))
                .foregroundStyle(Palette.slate)
                .frame(width: 38, height: 38)
                .background(Palette.soft, in: Circle())

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(person.name)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                    if person.guest {
                        Text("Guest")
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Palette.soft, in: Capsule())
                            .foregroundStyle(Palette.slate)
                            .fixedSize()
                    }
                }
                Text([person.relation, faces].compactMap { $0 }.joined(separator: " \u{00b7} "))
                    .font(.caption)
                    .foregroundStyle(person.faces == 0 ? Palette.steel : Palette.slate)
                    .lineLimit(1)
            }

            Spacer(minLength: 8)

            if let shirt = person.shirt {
                HStack(spacing: 5) {
                    ShirtSwatch(colour: ShirtColour(rawValue: shirt))
                    Text(shirt.capitalized)
                        .font(.caption)
                        .foregroundStyle(Palette.steel)
                        .lineLimit(1)
                        .fixedSize()
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Usually wears \(shirt)")
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Add a person
// ---------------------------------------------------------------------------

struct AddPersonSheet: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var relation = ""
    @State private var shirt: ShirtColour?
    @State private var picks: [PhotosPickerItem] = []
    @State private var photos: [PreparedPhoto] = []
    @State private var preparing = false
    @State private var saving = false
    @State private var problem: String?

    private static let maxPhotos = 3

    /// A picked photo, already downscaled: the bytes to send and a preview.
    private struct PreparedPhoto: Identifiable {
        let id = UUID()
        let jpeg: Data
        let preview: CGImage
    }

    private var trimmedName: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// The dog's own rule (`NAME_RE`): an ASCII letter first, then letters,
    /// spaces, apostrophes and hyphens, 40 at most. Checked here so a bad name
    /// is caught before any photo leaves the phone.
    private var nameProblem: String? {
        let candidate = trimmedName
        guard !candidate.isEmpty else { return nil }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz '-")
        let letters = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        guard candidate.count <= 40,
              candidate.unicodeScalars.allSatisfy(allowed.contains),
              candidate.unicodeScalars.first.map(letters.contains) == true else {
            return "Use plain letters, spaces, apostrophes or hyphens, up to 40, starting with a letter."
        }
        return nil
    }

    private var canSave: Bool { !trimmedName.isEmpty && nameProblem == nil && !saving && !preparing }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Text("Add a person")
                    .font(.annieHeading(20))
                    .foregroundStyle(Palette.ink)
                Spacer()
                Button(action: save) {
                    if saving {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Add").fontWeight(.semibold)
                    }
                }
                .disabled(!canSave)
                .keyboardShortcut(.defaultAction)
            }
            .tint(Palette.slate)
            .padding(.horizontal, 16)
            .padding(.vertical, 14)

            Rectangle().fill(Palette.line).frame(height: 1)

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 12) {
                        Eyebrow(text: "Who")
                        TextField("Name, e.g. Maya", text: $name)
                            .textFieldStyle(.roundedBorder)
                            .autocorrectionDisabled()
                            #if os(iOS)
                            .textInputAutocapitalization(.words)
                            #endif
                        if let nameProblem {
                            Text(nameProblem)
                                .font(.caption)
                                .foregroundStyle(Palette.alert)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        TextField("Relation, e.g. granddaughter, carer", text: $relation)
                            .textFieldStyle(.roundedBorder)
                    }
                    .annieCard()

                    VStack(alignment: .leading, spacing: 12) {
                        Eyebrow(text: "Usual shirt colour")
                        HStack(spacing: 0) {
                            swatchButton(nil, label: "None")
                            ForEach(ShirtColour.allCases) { colour in
                                swatchButton(colour, label: colour.label)
                            }
                        }
                        Text("Helps Annie tell people apart from across the room, before she can see a face.")
                            .font(.caption)
                            .foregroundStyle(Palette.steel)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .annieCard()

                    VStack(alignment: .leading, spacing: 12) {
                        Eyebrow(text: "Photos")
                        HStack(spacing: 10) {
                            ForEach(photos) { photo in
                                Image(decorative: photo.preview, scale: 1)
                                    .resizable()
                                    .scaledToFill()
                                    .frame(width: 72, height: 72)
                                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                                            .stroke(Palette.line, lineWidth: 1)
                                    )
                            }
                            PhotosPicker(selection: $picks, maxSelectionCount: Self.maxPhotos, matching: .images) {
                                VStack(spacing: 4) {
                                    if preparing {
                                        ProgressView().controlSize(.small)
                                    } else {
                                        Image(systemName: photos.isEmpty ? "photo.badge.plus" : "arrow.triangle.2.circlepath")
                                            .font(.system(size: 20, weight: .semibold))
                                    }
                                    Text(photos.isEmpty ? "Choose" : "Change")
                                        .font(.caption.weight(.semibold))
                                }
                                .foregroundStyle(Palette.slate)
                                .frame(width: 72, height: 72)
                                .background(Palette.soft, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                            }
                            .buttonStyle(.plain)
                            Spacer(minLength: 0)
                        }
                        Text("Up to \(Self.maxPhotos) clear, front-facing photos. They are shrunk on this phone, used once so Annie can learn the face, and not kept by the app, the server or the dog.")
                            .font(.caption)
                            .foregroundStyle(Palette.steel)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .annieCard()

                    if let problem {
                        Text(problem)
                            .font(.footnote)
                            .foregroundStyle(Palette.alert)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(16)
            }
            #if os(iOS)
            .scrollDismissesKeyboard(.interactively)
            #endif
        }
        .background(Palette.cream)
        .frame(minWidth: 320)
        .interactiveDismissDisabled(saving)
        .onChange(of: picks) { items in
            Task { await prepare(items) }
        }
    }

    private func swatchButton(_ colour: ShirtColour?, label: String) -> some View {
        let selected = shirt == colour
        return Button {
            shirt = colour
        } label: {
            VStack(spacing: 5) {
                ShirtSwatch(colour: colour, size: 28)
                    .padding(3)
                    .overlay(Circle().stroke(selected ? Palette.slate : Color.clear, lineWidth: 2))
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(selected ? Palette.ink : Palette.steel)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .frame(maxWidth: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(colour == nil ? "No shirt colour" : "\(label) shirt")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// Load and shrink what was picked. A photo that can't be read is left out
    /// and said so, rather than sent as something the dog can't use.
    private func prepare(_ items: [PhotosPickerItem]) async {
        preparing = true
        defer { preparing = false }
        var ready: [PreparedPhoto] = []
        var unreadable = 0
        for item in items.prefix(Self.maxPhotos) {
            guard let data = try? await item.loadTransferable(type: Data.self),
                  let jpeg = Photo.jpeg(from: data, maxPixel: 640, quality: 0.8),
                  let preview = Photo.cgImage(from: jpeg, maxPixel: 216) else {
                unreadable += 1
                continue
            }
            ready.append(PreparedPhoto(jpeg: jpeg, preview: preview))
        }
        photos = ready
        problem = unreadable == 0 ? nil
            : "\(unreadable) photo\(unreadable == 1 ? "" : "s") couldn't be read and \(unreadable == 1 ? "was" : "were") left out."
    }

    private func save() {
        guard canSave else { return }
        saving = true
        problem = nil
        let relationText = relation.trimmingCharacters(in: .whitespacesAndNewlines)
        let person = NewPerson(name: trimmedName,
                               relation: relationText.isEmpty ? nil : String(relationText.prefix(40)),
                               shirt: shirt?.rawValue,
                               notes: nil,
                               photos: photos.map { $0.jpeg.base64EncodedString() })
        Task {
            let failure = await state.addPerson(person)
            saving = false
            if let failure {
                problem = failure   // stay open: nothing typed or picked is lost
            } else {
                dismiss()
            }
        }
    }
}
