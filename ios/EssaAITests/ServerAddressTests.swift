import XCTest
@testable import EssaAI

/// The setup screen's address rules. The same cases as
/// android/tools/normalise_test.mjs — an address typed into either phone must
/// mean the same server. Worth a test because it is a guess with two opposite
/// right answers: on the LAN a bare address means http and port 8000, for a
/// deployed server https and 443, and getting it backwards reports a working
/// server as "not reachable".
final class ServerAddressTests: XCTestCase {

    func testNormalise() {
        let cases: [(String, String)] = [
            ("192.168.1.5", "http://192.168.1.5:8000"),
            ("192.168.1.5:8000/m", "http://192.168.1.5:8000"),
            ("http://192.168.1.5/m/", "http://192.168.1.5:8000"),
            ("  192.168.1.5  ", "http://192.168.1.5:8000"),
            ("localhost", "http://localhost:8000"),
            ("warehouse-pc", "http://warehouse-pc:8000"),             // bare machine name
            ("192.168.1.5:9000", "http://192.168.1.5:9000"),          // a moved port survives
            ("essa.vercel.app", "https://essa.vercel.app"),
            ("https://essa.vercel.app", "https://essa.vercel.app"),
            ("https://essa.vercel.app/m", "https://essa.vercel.app"),
            ("essa-intake.onrender.com/m/", "https://essa-intake.onrender.com"),
            ("https://essa.example.com:443", "https://essa.example.com:443"),
            ("http://essa.example.com", "http://essa.example.com:8000"),
            ("", ""),
            ("   ", ""),
            ("https:///", ""),
        ]
        for (input, want) in cases {
            XCTAssertEqual(ServerAddress.normalise(input), want, "normalise(\"\(input)\")")
        }
    }

    func testLanHost() {
        XCTAssertTrue(ServerAddress.isLanHost("192.168.0.7"))
        XCTAssertTrue(ServerAddress.isLanHost("warehouse-pc"))
        XCTAssertTrue(ServerAddress.isLanHost("localhost"))
        XCTAssertFalse(ServerAddress.isLanHost("essa.example.com"))
        XCTAssertFalse(ServerAddress.isLanHost("1.2.3.com"))
    }

    /// The scanner shim is what makes the page's scan button reach the native
    /// scanner; it has to post to the handler the controller registers.
    func testShimPostsToTheRegisteredHandler() {
        XCTAssertTrue(WebViewController.scannerShim.contains("messageHandlers.essaScan.postMessage"))
        XCTAssertTrue(WebViewController.scannerShim.contains("window.__essaScanResult"))
    }
}
