import UIKit

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        self.window = window
        // No address yet: ask for it. Otherwise straight into the warehouse.
        if let base = ServerAddress.saved, !base.isEmpty {
            show(WebViewController(base: base))
        } else {
            show(SetupViewController())
        }
        window.makeKeyAndVisible()
    }

    /// Swaps the whole screen — setup to warehouse, or back on "Change server".
    func show(_ controller: UIViewController) {
        guard let window else { return }
        window.rootViewController = controller
        UIView.transition(with: window, duration: 0.2, options: .transitionCrossDissolve, animations: nil)
    }
}

extension UIViewController {
    var sceneDelegate: SceneDelegate? {
        view.window?.windowScene?.delegate as? SceneDelegate
    }
}
