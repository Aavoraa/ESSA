import UIKit

/// The warehouse phone app for iPhone and iPad: a web view onto the server's own
/// /m screens, with scanning done natively — the same design as the Android app
/// (android/app/src/main/java/com/essa/warehouse/MainActivity.java).
///
/// The pages come from the server every time, so changing the mobile UI needs no
/// new app build, and Android, iPhone and the browser all show the same screens.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        true
    }
}
