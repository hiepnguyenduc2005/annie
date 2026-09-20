//
//  MessageRouter.swift
//  AnnieApp
//
//  Which path a typed line takes. Two things a family member says need the
//  family-message service (POST /api/messages): anything that asks Annie to
//  *tell*, *remind*, *check on* or *ask* someone — these are delivery intents,
//  and only the family run gives them a recorded outcome, a reminder
//  acknowledgement, an emergency alert and the resident's reply on the
//  timeline. Everything else (go, come, dance, explore — motion and operator
//  instructions) goes straight to the dog's situated agent, which is faster
//  and moves her at once.
//
//  Pure and tiny on purpose: submit() applies it, and the test harness in
//  Tests/MessageRouterHarness compiles this exact file and runs it.

import Foundation

enum MessageIntent: Equatable {
    case familyMessage
    case directInstruction
}

enum MessageRouter {
    /// Delivery verbs whose outcome must be recorded: a reply that must reach
    /// the timeline, a reminder that must be acknowledged, a concern that must
    /// be raised as an emergency. The direct path offers none of that.
    private static let familyOpeners = ["tell ", "remind ", "check on ", "ask "]

    static func route(_ text: String) -> MessageIntent {
        let lowered = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if familyOpeners.contains(where: { lowered.hasPrefix($0) }) {
            return .familyMessage
        }
        return .directInstruction
    }
}
