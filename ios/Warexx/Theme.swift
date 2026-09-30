import UIKit

/// The warehouse app's own colours — the same ones the /m pages and the Android
/// app use, so the native screens and the web screens read as one app.
enum Theme {
    static let chrome = UIColor(hex: 0x0B3D2E)       // the dark forest green header
    static let accent = UIColor(hex: 0x2F8F68)
    static let background = UIColor(hex: 0xF7F9F7)
    static let text = UIColor(hex: 0x17221D)
    static let muted = UIColor(hex: 0x68756E)
}

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: 1)
    }
}
