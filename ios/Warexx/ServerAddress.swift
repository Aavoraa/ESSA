import Foundation

/// Where the warehouse server is, and turning what someone typed into it.
///
/// The same rules as the Android app's SetupActivity.normalise / isLanHost, and
/// the same test cases (WarexxTests/ServerAddressTests.swift mirrors
/// android/tools/normalise_test.mjs), so one address typed into either phone
/// means the same server.
///
/// "192.168.1.5", "192.168.1.5:8000/m", "http://192.168.1.5/m/" all become
/// "http://192.168.1.5:8000", and "https://essa.example.com/m" becomes
/// "https://essa.example.com". On the shop's LAN a bare address means http on
/// port 8000; a deployed server is a domain, https, on 443 — and appending 8000
/// there would report a working server as unreachable.
enum ServerAddress {
    static let key = "base_url"

    static var saved: String? {
        get { UserDefaults.standard.string(forKey: key) }
        set { UserDefaults.standard.set(newValue, forKey: key) }
    }

    static func normalise(_ raw: String) -> String {
        var t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return "" }

        var scheme = ""                                 // "" = not stated, decide below
        if t.hasPrefix("http://") {
            scheme = "http://"
            t = String(t.dropFirst(7))
        } else if t.hasPrefix("https://") {
            scheme = "https://"
            t = String(t.dropFirst(8))
        }

        // Anything after the host:port — a path, a query, a fragment — is noise.
        // People copy the /m out of a browser's address bar.
        if let cut = t.firstIndex(where: { $0 == "/" || $0 == "?" || $0 == "#" }) {
            t = String(t[..<cut])
        }
        t = t.trimmingCharacters(in: .whitespaces)
        if t.isEmpty { return "" }

        let host = t.firstIndex(of: ":").map { String(t[..<$0]) } ?? t
        if scheme.isEmpty { scheme = isLanHost(host) ? "http://" : "https://" }

        // 8000 is what the warehouse server listens on; https needs nothing added.
        if !t.contains(":") && scheme == "http://" { t += ":8000" }
        return scheme + t
    }

    /// The PC in the office rather than a server on the internet: an IPv4
    /// address or a bare machine name. A guess, but a recoverable one — the
    /// address is checked against /api/status before it is saved.
    static func isLanHost(_ host: String) -> Bool {
        if host.isEmpty || host == "localhost" { return true }
        if !host.contains(".") { return true }         // a bare machine name
        return host.range(of: #"^\d{1,3}(\.\d{1,3}){3}$"#, options: .regularExpression) != nil
    }
}
