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
//  Questions about existing observations stay read-only. Polite requests such
//  as "could you check on Grandma?" are delivery requests despite the question
//  mark. submit() uses this same decision as Tests/MessageRouterChecks.swift.

import Foundation

enum MessageIntent: Equatable {
    case question
    case familyMessage
    case directInstruction
}

enum MessageRouter {
    /// Delivery verbs whose outcome must be recorded: a reply that must reach
    /// the timeline, a reminder that must be acknowledged, a concern that must
    /// be raised as an emergency. The direct path offers none of that.
    private static let familyOpeners = ["tell ", "remind ", "check on ", "ask "]
    private static let questionOpeners = ["where", "what", "when", "who", "why", "how", "which", "did", "does", "do", "is", "are",
                                         "was", "were", "has", "have", "had", "can", "could", "will", "would", "should"]

    // Only an explicit request to act on a resident can override a question.
    // "Could you tell me where Grandma is?" remains an observation lookup.
    private static let person = #"(?:(?:my|our)\s+)?(?:grandma|grandmother|granny|gran|nan|mom|mum|jeanine|janine|ellis|henry|her|him|them)\b"#
    private static let subject = #"(?:(?:my|our)\s+)?(?:grandma|grandmother|granny|gran|nan|mom|mum|jeanine|janine|ellis|henry|she|he|they)\b"#
    private static let delivery = #"(?:(?:tell|remind|ask)\s+"# + person
        + #"|check\s+(?:in\s+)?on\s+"# + person
        + #"|(?:make\s+sure|see\s+if)\s+(?:that\s+)?"# + subject + #")"#
    private static let polite = #"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+)"#

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }

    static func route(_ text: String) -> MessageIntent {
        let lowered = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .replacingOccurrences(of: #"^(?:(?:hey|hi|hello)\s+)?annie\b[\s,.!:;–—-]*"#, with: "", options: .regularExpression)
        if familyOpeners.contains(where: { lowered.hasPrefix($0) }) {
            return .familyMessage
        }
        if matches(lowered, "^" + polite + delivery) { return .familyMessage }
        // Historical/capability questions must not become commands because they
        // mention a delivery elsewhere in the sentence.
        if let first = lowered.split(whereSeparator: { !$0.isLetter }).first,
           questionOpeners.contains(String(first)) { return .question }
        // A family member may explain the concern before making their request.
        if matches(lowered, #"[,;.!]\s*"# + polite + delivery) { return .familyMessage }
        if lowered.hasSuffix("?") { return .question }
        return .directInstruction
    }
}
