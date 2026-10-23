import UIKit
import AVFoundation
import AudioToolbox

/// The native scanner the page's scan button opens.
///
/// AVFoundation's barcode reader rather than VisionKit's DataScanner: the
/// DataScanner needs an A12 chip and iOS 16, and this has to run on every phone
/// the app installs on. It reads the same five symbologies the web app asks for
/// (SCAN_FORMATS in backend/app/mobile/index.html) and the Android scanner reads,
/// so a label that scans on one phone scans on all of them.
final class ScannerViewController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {

    /// The decoded text, or nil when cancelled. Called once, after the scanner
    /// has closed.
    var onResult: ((String?) -> Void)?

    private static let formats: [AVMetadataObject.ObjectType] = [.qr, .code128, .ean13, .ean8, .code39]

    private let session = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "essaai.scanner")
    private var preview: AVCaptureVideoPreviewLayer?
    private var finished = false

    private let frameBox = UIView()
    private let hint = UILabel()
    private let settingsButton = UIButton(type: .system)

    init() {
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .fullScreen
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        buildOverlay()

        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configure()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { granted in
                DispatchQueue.main.async {
                    if granted { self.configure() } else { self.cameraOff() }
                }
            }
        default:
            cameraOff()
        }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
        // An iPad turned on its side: keep the picture the right way up.
        if let connection = preview?.connection, connection.isVideoOrientationSupported,
           let o = view.window?.windowScene?.interfaceOrientation,
           let video = AVCaptureVideoOrientation(interface: o) {
            connection.videoOrientation = video
        }
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        stopSession()
    }

    // MARK: camera

    private func configure() {
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else {
            return unavailable("This device has no camera the scanner can use. Type the code instead.")
        }
        session.beginConfiguration()
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            return unavailable("The camera could not be started. Type the code instead.")
        }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        // only after the output is attached does it know what it can read
        output.metadataObjectTypes = Self.formats.filter { output.availableMetadataObjectTypes.contains($0) }
        session.commitConfiguration()

        // Labels are read from a hand's length away: favour near focus.
        if (try? device.lockForConfiguration()) != nil {
            if device.isAutoFocusRangeRestrictionSupported { device.autoFocusRangeRestriction = .near }
            if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
            device.unlockForConfiguration()
        }

        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        layer.frame = view.bounds
        view.layer.insertSublayer(layer, at: 0)
        preview = layer

        // startRunning blocks for a moment; never on the main thread
        sessionQueue.async { self.session.startRunning() }
    }

    private func stopSession() {
        sessionQueue.async { if self.session.isRunning { self.session.stopRunning() } }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !finished,
              let code = metadataObjects
                .compactMap({ ($0 as? AVMetadataMachineReadableCodeObject)?.stringValue })
                .first(where: { !$0.isEmpty }) else { return }
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        AudioServicesPlaySystemSound(1057)                 // the short "tock" — Android beeps too
        finish(code)
    }

    @objc private func cancelTapped() { finish(nil) }

    private func finish(_ code: String?) {
        guard !finished else { return }
        finished = true
        stopSession()
        let callback = onResult
        dismiss(animated: true) { callback?(code) }
    }

    // MARK: no camera

    private func cameraOff() {
        unavailable("Camera access is off for ESSA AI. Turn it on in Settings › ESSA AI › Camera, "
                    + "or type the code instead.")
        settingsButton.isHidden = false
    }

    private func unavailable(_ message: String) {
        frameBox.isHidden = true
        hint.text = message
    }

    @objc private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
    }

    // MARK: overlay

    private func buildOverlay() {
        frameBox.layer.borderColor = UIColor.white.withAlphaComponent(0.9).cgColor
        frameBox.layer.borderWidth = 3
        frameBox.layer.cornerRadius = 16
        frameBox.isUserInteractionEnabled = false

        hint.text = "Point at the QR or barcode on the label"
        hint.textColor = .white
        hint.font = .preferredFont(forTextStyle: .body)
        hint.adjustsFontForContentSizeCategory = true
        hint.numberOfLines = 0
        hint.textAlignment = .center
        hint.layer.shadowColor = UIColor.black.cgColor
        hint.layer.shadowOpacity = 0.8
        hint.layer.shadowRadius = 4
        hint.layer.shadowOffset = .zero

        var cancelCfg = UIButton.Configuration.filled()
        cancelCfg.title = "Cancel"
        cancelCfg.baseBackgroundColor = .white
        cancelCfg.baseForegroundColor = Theme.text
        cancelCfg.cornerStyle = .large
        cancelCfg.contentInsets = NSDirectionalEdgeInsets(top: 13, leading: 30, bottom: 13, trailing: 30)
        let cancelButton = UIButton(configuration: cancelCfg)
        cancelButton.addTarget(self, action: #selector(cancelTapped), for: .touchUpInside)

        var settingsCfg = UIButton.Configuration.filled()
        settingsCfg.title = "Open Settings"
        settingsCfg.baseBackgroundColor = Theme.accent
        settingsCfg.cornerStyle = .large
        settingsButton.configuration = settingsCfg
        settingsButton.isHidden = true
        settingsButton.addTarget(self, action: #selector(openSettings), for: .touchUpInside)

        let buttons = UIStackView(arrangedSubviews: [settingsButton, cancelButton])
        buttons.axis = .vertical
        buttons.spacing = 12
        buttons.alignment = .center

        for v in [frameBox, hint, buttons] as [UIView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(v)
        }
        let safe = view.safeAreaLayoutGuide
        // Square, 68% of the width, never more than 320pt, and never taller than
        // 60% of the screen — an iPad in landscape has height to spare, a small
        // phone does not.
        let wide = frameBox.widthAnchor.constraint(equalTo: safe.widthAnchor, multiplier: 0.68)
        wide.priority = .defaultHigh
        NSLayoutConstraint.activate([
            frameBox.centerXAnchor.constraint(equalTo: safe.centerXAnchor),
            frameBox.centerYAnchor.constraint(equalTo: safe.centerYAnchor, constant: -30),
            frameBox.heightAnchor.constraint(equalTo: frameBox.widthAnchor),
            frameBox.widthAnchor.constraint(lessThanOrEqualToConstant: 320),
            frameBox.heightAnchor.constraint(lessThanOrEqualTo: safe.heightAnchor, multiplier: 0.6),
            wide,

            hint.leadingAnchor.constraint(equalTo: safe.leadingAnchor, constant: 24),
            hint.trailingAnchor.constraint(equalTo: safe.trailingAnchor, constant: -24),
            hint.bottomAnchor.constraint(equalTo: buttons.topAnchor, constant: -24),
            hint.topAnchor.constraint(greaterThanOrEqualTo: frameBox.bottomAnchor, constant: 12),

            buttons.centerXAnchor.constraint(equalTo: safe.centerXAnchor),
            buttons.bottomAnchor.constraint(equalTo: safe.bottomAnchor, constant: -24),
        ])
    }
}

private extension AVCaptureVideoOrientation {
    /// UIInterfaceOrientation and AVCaptureVideoOrientation name the same four
    /// positions the same way (by where the home button would be).
    init?(interface: UIInterfaceOrientation) {
        switch interface {
        case .portrait: self = .portrait
        case .portraitUpsideDown: self = .portraitUpsideDown
        case .landscapeLeft: self = .landscapeLeft
        case .landscapeRight: self = .landscapeRight
        default: return nil
        }
    }
}
