//  MessageRouterChecks.swift
//
//  Standalone check that the ACTUAL production router behaves as required.
//  Compiled together with Sources/AnnieApp/MessageRouter.swift by:
//
//    cd app_frontend
//    swiftc -enable-testing Tests/MessageRouterChecks.swift \
//           Sources/AnnieApp/MessageRouter.swift -o /tmp/message_router_checks
//    /tmp/message_router_checks
//
//  Prints PASS lines and exits 0 when every case routes correctly; exits 1
//  otherwise. No SwiftPM test target, no mirror of the rule.

import Foundation

@main
struct MessageRouterChecks {
    static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
        exit(1)
    }

    static func expect(_ route: MessageIntent, _ expected: MessageIntent, _ input: String) {
        guard route == expected else {
            fail("\(input.debugDescription) routed \(route), expected \(expected)")
        }
        print("PASS: \(input.debugDescription) -> \(route)")
    }

    static func main() {
        // Delivery intents must reach the family-message path (three outcomes,
        // resident reply, persistence).
        expect(MessageRouter.route("Tell Grandma to plug in her phone"), .familyMessage, "Tell Grandma to plug in her phone")
        expect(MessageRouter.route("tell grandma it's lunch"), .familyMessage, "tell grandma it's lunch")
        expect(MessageRouter.route("Remind her about the vet"), .familyMessage, "Remind her about the vet")
        expect(MessageRouter.route("  remind me to stretch"), .familyMessage, "  remind me to stretch")
        expect(MessageRouter.route("Check on Grandma"), .familyMessage, "Check on Grandma")
        expect(MessageRouter.route("check on her"), .familyMessage, "check on her")
        expect(MessageRouter.route("Ask Jeanine if she needs anything"), .familyMessage, "Ask Jeanine if she needs anything")
        expect(MessageRouter.route("ask her where the phone is"), .familyMessage, "ask her where the phone is")

        // Motion and operator instructions stay direct to the dog.
        expect(MessageRouter.route("Go wave at Grandma"), .directInstruction, "Go wave at Grandma")
        expect(MessageRouter.route("go explore"), .directInstruction, "go explore")
        expect(MessageRouter.route("Come home"), .directInstruction, "Come home")
        expect(MessageRouter.route("dance"), .directInstruction, "dance")
        expect(MessageRouter.route("sit"), .directInstruction, "sit")
        expect(MessageRouter.route("turn around"), .directInstruction, "turn around")

        // Word boundaries: telling/teller are not delivery asks.
        expect(MessageRouter.route("telling me a story"), .directInstruction, "telling me a story")
        expect(MessageRouter.route("Teller tricks"), .directInstruction, "Teller tricks")

        print("MessageRouterChecks: all cases passed (actual MessageRouter.swift compiled).")
    }
}
