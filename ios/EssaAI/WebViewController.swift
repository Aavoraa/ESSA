import UIKit
import WebKit

/// The warehouse's /m screens, live from the server, with one thing done
/// natively — scanning.
///
/// Why scanning cannot be left to the page: it scans with `BarcodeDetector`,
/// which WebKit (every browser on iOS) does not implement at all, and the camera
/// is refused outright to a page served over plain http, which the shop's LAN
/// server is. So, exactly as the Android app does, the page is left alone and
/// `scannerShim` replaces its `openScanner` with one that opens a native scanner
/// and hands the decoded text back.
///
/// That shim reaches into the page's own globals (openScanner, resolveCode, S,
/// render, toast). Rename one in backend/app/mobile/index.html and the scan
/// button quietly stops working — `node ios/tools/shim_test.mjs` checks for it,
/// and CI runs it before every build.
final class WebViewController: UIViewController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {

    /// Injected at the end of every page load. The same shim as
    /// MainActivity.INJECTED_SCANNER_SHIM on Android, posting to a WebKit message
    /// handler instead of a Java bridge. The stub BarcodeDetector is there because
    /// the page checks `'BarcodeDetector' in window` before taking the scanner
    /// path at all.
    static let scannerShim = """
    (function(){
      if (window.__essaNativeScan) return;
      window.__essaNativeScan = true;
      if (!('BarcodeDetector' in window)) { window.BarcodeDetector = function(){}; }
      var pending = null;
      window.openScanner = function(onCode){
        pending = onCode || null;
        window.webkit.messageHandlers.essaScan.postMessage('scan');
      };
      window.closeScanner = function(){};
      window.__essaScanResult = async function(code){
        var cb = pending; pending = null;
        if (!code) return;
        if (cb) return cb(code);
        var p = await resolveCode(code);
        if (p) { S.selected = p; S.detailBack = 'list'; S.screen = 'detail'; render(); }
        else { toast('Scanned, but no product matches that code'); }
      };
    })();
    """

    private let base: String
    private var web: WKWebView!
    private var unreachablePending = false

    init(base: String) {
        self.base = base
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    // The status bar sits on the header's dark green, as on Android.
    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Theme.chrome

        let content = WKUserContentController()
        content.addUserScript(WKUserScript(source: Self.scannerShim,
                                           injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        content.add(WeakScriptHandler(self), name: "essaScan")

        let config = WKWebViewConfiguration()
        config.userContentController = content
        config.websiteDataStore = .default()             // the page keeps its sign-in in localStorage
        config.allowsInlineMediaPlayback = true          // the in-page camera preview (https servers)
        config.mediaTypesRequiringUserActionForPlayback = []

        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true   // swipe back, like Android's back button
        // The page lays itself out around the notch and home bar with
        // env(safe-area-inset-*) — letting the scroll view inset as well would
        // pad everything twice.
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.isOpaque = false
        web.backgroundColor = Theme.background
        web.scrollView.backgroundColor = Theme.background
        web.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(web)

        // Top at the safe area, so the status bar keeps the green band behind it;
        // sides and bottom to the edges, where the page's own safe-area padding
        // keeps content off the home bar and (in landscape) the notch.
        NSLayoutConstraint.activate([
            web.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            web.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            web.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        load()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if unreachablePending { showUnreachable() }
    }

    private func load() {
        guard let url = URL(string: base + "/m") else { return showUnreachable() }
        web.load(URLRequest(url: url))
    }

    private var serverHost: String? { URL(string: base)?.host }

    // MARK: scanning

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "essaScan", presentedViewController == nil else { return }
        let scanner = ScannerViewController()
        scanner.onResult = { [weak self] code in self?.deliver(code) }
        present(scanner, animated: true)
    }

    /// Hands the scan to the page. JSON-encoded, because a code is arbitrary
    /// text — a quote or a backslash in a QR payload must not break the call.
    /// A cancel is delivered as null, which clears the page's pending callback.
    private func deliver(_ code: String?) {
        var arg = "null"
        if let code, let data = try? JSONSerialization.data(withJSONObject: [code]),
           let json = String(data: data, encoding: .utf8) {
            arg = json + "[0]"
        }
        web.evaluateJavaScript("void (window.__essaScanResult && window.__essaScanResult(\(arg)))",
                               completionHandler: nil)
    }

    // MARK: navigation

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    /// Only the page itself is worth interrupting for — these fire for the main
    /// frame only, so a failed icon or API poll never throws up a dialog mid-shift.
    private func failed(_ error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }   // superseded by another load
        if e.domain == "WebKitErrorDomain" && e.code == 102 { return }                // handed to a download / another app
        showUnreachable()
    }

    /// iOS reclaims a background web view's memory; coming back to a blank
    /// screen would look like the app had died.
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    /// tel:, mailto: and anything else that is not a web page goes to the app
    /// that handles it.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let scheme = navigationAction.request.url?.scheme?.lowercased() ?? ""
        if ["http", "https", "about", "blob", "data"].contains(scheme) || scheme.isEmpty {
            decisionHandler(.allow)
            return
        }
        if let url = navigationAction.request.url { UIApplication.shared.open(url) }
        decisionHandler(.cancel)
    }

    /// A link or window.open that wants a new window — a label sheet, a printout.
    /// Opened over the app in a second web view that shares this one's session,
    /// so it is signed in, and closing it returns to the screen exactly as left.
    /// Another site's page goes to Safari instead.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // No host is a blank window the page is about to write into — keep it.
        if let url = navigationAction.request.url, let host = url.host, host != serverHost {
            UIApplication.shared.open(url)
            return nil
        }
        let popup = PopupViewController(configuration: configuration)
        topPresenter.present(UINavigationController(rootViewController: popup), animated: true)
        return popup.web
    }

    // MARK: camera for the page itself

    /// The page asking for the camera (getUserMedia) — the in-page photo capture,
    /// which WebKit only offers on an https server. Granted for the warehouse's
    /// own pages; iOS still asks the person once for the app as a whole.
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == serverHost ? .grant : .deny)
    }

    // MARK: alert(), confirm(), prompt()

    // WKWebView shows none of these by itself: without them alert() is silent,
    // confirm() answers "no" and prompt() answers nothing — so "Post this GRN?"
    // could never be said yes to.

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        presentPanel(a) { completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        let ok = UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) }
        a.addAction(ok)
        a.preferredAction = ok
        presentPanel(a) { completionHandler(false) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        a.addTextField { tf in
            tf.text = defaultText
            tf.autocapitalizationType = .none
            tf.autocorrectionType = .no
        }
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
        let ok = UIAlertAction(title: "OK", style: .default) { [weak a] _ in
            completionHandler(a?.textFields?.first?.text ?? "")
        }
        a.addAction(ok)
        a.preferredAction = ok
        presentPanel(a) { completionHandler(nil) }
    }

    /// Every panel must answer exactly once, or the page hangs waiting for it —
    /// so if there is nowhere to show it, it is answered with the "cancel" value.
    private func presentPanel(_ alert: UIAlertController, orElse fallback: @escaping () -> Void) {
        guard view.window != nil else { return fallback() }
        topPresenter.present(alert, animated: true)
    }

    private var topPresenter: UIViewController {
        var top: UIViewController = self
        while let next = top.presentedViewController, !next.isBeingDismissed { top = next }
        return top
    }

    // MARK: server not reachable

    private func showUnreachable() {
        // Not on screen yet (the very first load failed): show it once it is.
        guard view.window != nil else { unreachablePending = true; return }
        unreachablePending = false
        guard presentedViewController == nil else { return }
        let a = UIAlertController(
            title: "Warehouse not reachable",
            message: "Couldn’t load \(base). The server may be off, or the iPhone may be on a different network.",
            preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Change server", style: .default) { [weak self] _ in
            self?.sceneDelegate?.show(SetupViewController())
        })
        let retry = UIAlertAction(title: "Retry", style: .default) { [weak self] _ in self?.load() }
        a.addAction(retry)
        a.preferredAction = retry
        present(a, animated: true)
    }
}

/// A page the warehouse opened in a new window, shown over the app with Done.
final class PopupViewController: UIViewController, WKUIDelegate {
    let web: WKWebView

    init(configuration: WKWebViewConfiguration) {
        web = WKWebView(frame: .zero, configuration: configuration)
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func loadView() {
        web.uiDelegate = self
        view = web
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        navigationItem.rightBarButtonItem = UIBarButtonItem(barButtonSystemItem: .done, target: self,
                                                            action: #selector(close))
        // Share / print what is on screen — a label sheet is usually opened to print it.
        navigationItem.leftBarButtonItem = UIBarButtonItem(barButtonSystemItem: .action, target: self,
                                                           action: #selector(share))
    }

    @objc private func close() { dismiss(animated: true) }

    @objc private func share() {
        var items: [Any] = [web.viewPrintFormatter()]
        if let url = web.url { items.append(url) }
        let sheet = UIActivityViewController(activityItems: items, applicationActivities: nil)
        sheet.popoverPresentationController?.barButtonItem = navigationItem.leftBarButtonItem   // iPad
        present(sheet, animated: true)
    }

    // window.close() from the page
    func webViewDidClose(_ webView: WKWebView) { dismiss(animated: true) }
}

/// WKUserContentController holds its message handlers strongly; handing it the
/// controller itself would keep the web view alive for ever after "Change server".
final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    private weak var target: WKScriptMessageHandler?

    init(_ target: WKScriptMessageHandler) { self.target = target }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(controller, didReceive: message)
    }
}
