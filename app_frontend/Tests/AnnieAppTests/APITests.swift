import XCTest
@testable import AnnieApp

final class StubProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, Data))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, data) = try Self.handler!(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

final class APITests: XCTestCase {
    func api() -> AnnieAPI {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return AnnieAPI(baseURL: URL(string: "http://localhost:8000")!, session: URLSession(configuration: config))
    }

    func testTodayConversationPinsBackendDayDuringPagination() async throws {
        var calls = 0
        StubProtocol.handler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            let query = Dictionary(uniqueKeysWithValues: components.queryItems!.map { ($0.name, $0.value!) })
            XCTAssertEqual(query["app_user_id"], "2")
            XCTAssertEqual(components.path, "/api/messages")
            calls += 1
            if calls == 1 { XCTAssertNil(query["day"]) }
            else { XCTAssertEqual(query["day"], "2026-09-20"); XCTAssertEqual(query["cursor"], "a+b/=") }
            let json: [String: Any] = ["id": 1, "app_user_id": 2, "dog_user_id": 1, "day": "2026-09-20", "messages": [], "next_cursor": calls == 1 ? "a+b/=" : NSNull()]
            return (200, try JSONSerialization.data(withJSONObject: json))
        }
        let conversation = try await api().conversation(userID: 2)
        XCTAssertEqual(calls, 2)
        XCTAssertEqual(conversation.day, "2026-09-20")
    }

    func testPostCarriesStableKeyWithoutBearerToken() async throws {
        StubProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/api/messages")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Idempotency-Key"), "stable-key")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            return (202, Data("{\"conversation_id\":1,\"message_id\":2,\"request_id\":3,\"day\":\"2026-09-20\",\"status\":\"queued\"}".utf8))
        }
        let receipt = try await api().sendMessage(NewMessage(app_user_id: 2, text: "Hello"), key: "stable-key")
        XCTAssertEqual(receipt.request_id, 3)
    }

    func testServerErrorIsNotDecodedAsAnEmptyConversation() async {
        StubProtocol.handler = { _ in (503, Data("{}".utf8)) }
        do {
            _ = try await api().conversation(userID: 2)
            XCTFail("Should fail")
        } catch APIError.badStatus(let status) { XCTAssertEqual(status, 503) }
        catch { XCTFail("Unexpected error: \(error)") }
    }
}
