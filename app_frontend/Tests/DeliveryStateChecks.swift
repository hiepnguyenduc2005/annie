// Compile with Sources/AnnieApp/Models.swift. No server or paid API needed.
import Foundation

@main
struct DeliveryStateChecks {
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else {
            FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
            exit(1)
        }
        print("PASS: \(message)")
    }

    static func main() throws {
        let decoder = JSONDecoder()
        func run(_ status: String, event: String? = nil) throws -> FamilyRun {
            var object: [String: Any] = ["run_id": "r1", "author_id": "zach", "text": "Charge your phone", "created_at": 1, "status": status, "events": []]
            if let event {
                object["events"] = [["event_id": "e1", "kind": event, "at": 1, "summary": "Receipt", "speaker": "annie"]]
            }
            return try decoder.decode(FamilyRun.self, from: JSONSerialization.data(withJSONObject: object))
        }
        let pending = try run("running")
        expect(pending.statusLabel == "Waiting for execution confirmation", "HTTP acceptance never claims execution")
        expect(pending.reminder_id == nil, "Legacy runs decode without a reminder ID")
        let queued = try run("queued")
        expect(!queued.finished && queued.statusLabel.contains("Queued"), "Queued run remains pending")
        let speaking = try run("running", event: "speaking")
        expect(speaking.statusLabel == "Speaking to Jeanine", "Execution receipt drives visible progress")
        let cancelled = try run("cancelled")
        expect(cancelled.finished && cancelled.statusLabel.contains("cancelled"), "Cancelled run stops polling and permits a new request")
        let expired = try run("unknown")
        expect(expired.finished && !expired.statusLabel.contains("Delivered"), "Missing receipt never becomes delivered")
        let message = NewMessage(author_id: "zach", text: "Charge your phone", reminder_id: 7)
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(message)) as! [String: Any]
        expect(encoded["reminder_id"] as? Int == 7, "Reminder ID reaches the backend")
        let camera = try decoder.decode(DogStatus.self, from: Data(#"{"available":true,"connected":true,"motion_enabled":false,"paused":true}"#.utf8))
        expect(camera.modeLabel == "Camera only · movement disabled", "Connected camera cannot imply movement is enabled")
        let paused = try decoder.decode(DogStatus.self, from: Data(#"{"available":true,"connected":true,"motion_enabled":true,"paused":true}"#.utf8))
        expect(paused.modeLabel == "Paused · ready for a new request", "Paused hardware waits for an explicit new request")
        let legacy = try decoder.decode(DogStatus.self, from: Data(#"{"available":true,"connected":true,"source":"simulation"}"#.utf8))
        expect(legacy.motion_enabled == nil && legacy.isSimulated, "Legacy simulator telemetry remains compatible and labeled")
        let pauseReceipt = try decoder.decode(FamilyPauseReceipt.self, from: Data(#"{"paused":true,"cancelled_runs":["r1"],"stop_confirmed":false}"#.utf8))
        expect(!pauseReceipt.stop_confirmed && pauseReceipt.cancelled_runs == ["r1"], "Task cancellation remains distinct from confirmed body stop")
        let snapshotData = try JSONSerialization.data(withJSONObject: ["thread": [], "runs": [
            ["run_id": "external", "author_id": "ellis", "text": "Charge your phone", "status": "queued", "created_at": 2, "events": [], "reminder_id": 4]
        ]])
        let snapshot = try decoder.decode(FamilySnapshot.self, from: snapshotData)
        expect(snapshot.runs.first?.reminder_id == 4 && snapshot.runs.first?.author_id == "ellis", "Another family client's reminder is decoded for the same screen")
    }
}
