import UIKit

/// Where the phone is told which machine the warehouse runs on.
///
/// Asked once and remembered, rather than built into the app, because the PC's
/// address is a DHCP lease — it changes when the router reboots, and one app has
/// to serve every phone in the shop. The address is checked before it is saved:
/// a typo caught here is a message on this screen, not an app that opens to a
/// blank page tomorrow morning. The Android app's SetupActivity does the same.
final class SetupViewController: UIViewController, UITextFieldDelegate {

    private let field = UITextField()
    private let button = UIButton(type: .system)
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let status = UILabel()

    override var preferredStatusBarStyle: UIStatusBarStyle { .darkContent }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Theme.background

        let title = UILabel()
        title.text = "Connect to the warehouse"
        title.font = .systemFont(ofSize: 24, weight: .bold)
        title.textColor = Theme.text
        title.numberOfLines = 0
        title.adjustsFontForContentSizeCategory = true

        // No example address: the Android app's first version offered one, and
        // it got typed in nearly verbatim instead of the PC's real address.
        let help = UILabel()
        help.text = "Enter the address of the PC running the warehouse server — its IPv4 address, "
            + "which running ipconfig on that PC will show. Address and port only; leave off /m "
            + "or any other path. The iPhone must be on the same Wi‑Fi.\n\n"
            + "For an online server, enter its web address instead."
        help.font = .preferredFont(forTextStyle: .subheadline)
        help.adjustsFontForContentSizeCategory = true
        help.textColor = Theme.muted
        help.numberOfLines = 0

        field.placeholder = "the PC address, e.g. 192.168.0.7:8000"
        field.text = ServerAddress.saved
        field.borderStyle = .roundedRect
        field.backgroundColor = .white
        field.textColor = Theme.text
        field.font = .systemFont(ofSize: 17)
        field.keyboardType = .URL
        field.textContentType = .URL
        field.autocapitalizationType = .none
        field.autocorrectionType = .no
        field.spellCheckingType = .no
        field.returnKeyType = .go
        field.clearButtonMode = .whileEditing
        field.delegate = self
        field.heightAnchor.constraint(greaterThanOrEqualToConstant: 48).isActive = true

        var cfg = UIButton.Configuration.filled()
        cfg.title = "Connect"
        cfg.baseBackgroundColor = Theme.accent
        cfg.baseForegroundColor = .white
        cfg.cornerStyle = .medium
        button.configuration = cfg
        button.heightAnchor.constraint(greaterThanOrEqualToConstant: 50).isActive = true
        button.addTarget(self, action: #selector(connect), for: .touchUpInside)

        spinner.color = Theme.accent
        spinner.hidesWhenStopped = true

        status.font = .preferredFont(forTextStyle: .footnote)
        status.adjustsFontForContentSizeCategory = true
        status.textColor = Theme.muted
        status.textAlignment = .center
        status.numberOfLines = 0

        let stack = UIStackView(arrangedSubviews: [title, help, field, button, spinner, status])
        stack.axis = .vertical
        stack.spacing = 14
        stack.setCustomSpacing(22, after: help)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let scroll = UIScrollView()
        scroll.keyboardDismissMode = .interactive
        scroll.alwaysBounceVertical = true
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(stack)
        view.addSubview(scroll)

        let content = scroll.contentLayoutGuide
        let frame = scroll.frameLayoutGuide
        // A comfortable column on an iPad, edge to edge (less margins) on a phone.
        let fullWidth = stack.widthAnchor.constraint(equalTo: frame.widthAnchor, constant: -48)
        fullWidth.priority = .defaultHigh
        // Centred vertically while it fits; scrolls when it does not (a small
        // phone with the keyboard up, or very large text).
        let centred = stack.centerYAnchor.constraint(equalTo: frame.centerYAnchor)
        centred.priority = .defaultLow
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: view.keyboardLayoutGuide.topAnchor),
            stack.centerXAnchor.constraint(equalTo: frame.centerXAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 480),
            fullWidth,
            stack.topAnchor.constraint(greaterThanOrEqualTo: content.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -24),
            centred,
        ])
    }

    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        connect()
        return true
    }

    @objc private func connect() {
        view.endEditing(true)
        let base = ServerAddress.normalise(field.text ?? "")
        guard !base.isEmpty else {
            status.text = "Enter the address first."
            return
        }
        busy(true)
        probe(base) { [weak self] problem in
            guard let self else { return }
            self.busy(false)
            if let problem {
                var msg = "Couldn’t reach the warehouse (\(problem)). Check the address, that the "
                    + "server is running, and that both are on the same Wi‑Fi."
                // The first request to a LAN address raises iOS's local-network
                // prompt, and fails while it is on screen. Saying so turns "it is
                // broken" into "tap Allow, then Connect again".
                if base.hasPrefix("http://") {
                    msg += "\n\nIf iPhone asked to find devices on your local network, tap Allow and "
                        + "try again. (Settings › Warexx › Local Network.)"
                }
                self.status.text = msg
                return
            }
            ServerAddress.saved = base
            self.sceneDelegate?.show(WebViewController(base: base))
        }
    }

    /// nil when the warehouse answered; otherwise why it didn't.
    private func probe(_ base: String, done: @escaping (String?) -> Void) {
        guard let url = URL(string: base + "/api/status") else {
            done("not a valid address")
            return
        }
        let req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 6)
        URLSession.shared.dataTask(with: req) { _, response, error in
            let problem: String?
            if let e = error as? URLError {
                switch e.code {
                case .timedOut: problem = "timed out"
                case .cannotConnectToHost: problem = "connection refused"
                case .cannotFindHost, .dnsLookupFailed: problem = "address not found"
                case .notConnectedToInternet: problem = "no network"
                case .appTransportSecurityRequiresSecureConnection: problem = "insecure address blocked"
                case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate:
                    problem = "certificate problem"
                default: problem = e.localizedDescription
                }
            } else if let error {
                problem = error.localizedDescription
            } else if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                problem = "HTTP \(http.statusCode)"
            } else {
                problem = nil
            }
            DispatchQueue.main.async { done(problem) }
        }.resume()
    }

    private func busy(_ on: Bool) {
        button.isEnabled = !on
        field.isEnabled = !on
        if on {
            spinner.startAnimating()
            status.text = "Checking…"
        } else {
            spinner.stopAnimating()
        }
    }
}
