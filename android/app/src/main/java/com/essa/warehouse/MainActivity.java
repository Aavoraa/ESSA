package com.essa.warehouse;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.provider.MediaStore;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceError;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;

import java.io.File;

import com.journeyapps.barcodescanner.ScanContract;
import com.journeyapps.barcodescanner.ScanOptions;

import org.json.JSONObject;

/**
 * The warehouse phone app: a WebView onto the server's own /m screens, with one
 * thing done natively — scanning.
 *
 * Why scanning cannot be left to the page. The web app scans with
 * `navigator.mediaDevices.getUserMedia` and `BarcodeDetector`, and Chromium only
 * exposes those on a secure origin. `http://192.168.x.x:8000` is not one, so in
 * a WebView `mediaDevices` is simply undefined and the camera button does
 * nothing. The usual answers are worse than this one: serving the warehouse over
 * HTTPS means a self-signed certificate and an app taught to accept it, and
 * bundling the pages into the APK means rebuilding it every time the mobile UI
 * changes. Instead the page keeps being served live from the server, and
 * INJECTED_SCANNER_SHIM redirects its scanner to a native one.
 */
public class MainActivity extends AppCompatActivity {

    static final String PREFS = "essa";
    static final String KEY_BASE_URL = "base_url";

    private WebView web;
    private ActivityResultLauncher<ScanOptions> scanLauncher;

    // The page's file input, held between opening the chooser and the result
    // coming back. Exactly one call to it per chooser — see fileChooserLauncher.
    private ValueCallback<Uri[]> fileCallback;
    private ActivityResultLauncher<Intent> fileChooserLauncher;
    // Where the camera app was told to write the photo. The camera returns an
    // empty result on success, so this is the only record of where it went.
    private Uri cameraOutput;
    private ActivityResultLauncher<String> cameraPermissionLauncher;
    private PermissionRequest pendingPagePermission;

    /**
     * Replaces the page's camera scanner with the native one.
     *
     * Two guards in the page read `'BarcodeDetector' in window` and fall back to
     * a text prompt when it is missing, so a stub has to exist for them to take
     * the scanner path at all. `openScanner` is then replaced wholesale. Its
     * no-callback behaviour — resolve the code and open that product — is
     * repeated here against the page's own globals, so both ways of scanning
     * behave exactly as they do in a browser.
     */
    private static final String INJECTED_SCANNER_SHIM =
        "(function(){" +
        "  if (window.__essaNativeScan) return;" +
        "  window.__essaNativeScan = true;" +
        "  if (!('BarcodeDetector' in window)) { window.BarcodeDetector = function(){}; }" +
        "  var pending = null;" +
        "  window.openScanner = function(onCode){ pending = onCode || null; AndroidHost.scan(); };" +
        "  window.closeScanner = function(){};" +
        "  window.__essaScanResult = async function(code){" +
        "    var cb = pending; pending = null;" +
        "    if (!code) return;" +
        "    if (cb) return cb(code);" +
        "    var p = await resolveCode(code);" +
        "    if (p) { S.selected = p; S.detailBack = 'list'; S.screen = 'detail'; render(); }" +
        "    else { toast('Scanned, but no product matches that code'); }" +
        "  };" +
        "})();";

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Portrait on a phone, where the screens are laid out for one hand; free
        // on a tablet (600dp and up), which sits in a stand or is held sideways.
        if (getResources().getConfiguration().smallestScreenWidthDp < 600) {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
        }

        String base = baseUrl();
        if (base == null || base.isEmpty()) {
            startActivity(new Intent(this, SetupActivity.class));
            finish();
            return;
        }

        setContentView(R.layout.activity_main);
        web = findViewById(R.id.web);

        // The other half of onShowFileChooser: whatever the picker or the camera
        // returns is handed back to the page's file input here. Every path out of
        // this — a file, several files, a photo just taken, or a cancel — must
        // call the callback exactly once, because a WebView whose chooser never
        // answers will not open another one for the rest of the session.
        fileChooserLauncher = registerForActivityResult(
            new ActivityResultContracts.StartActivityForResult(), result -> {
                ValueCallback<Uri[]> cb = fileCallback;
                fileCallback = null;
                if (cb == null) return;

                Uri[] out = null;
                Intent data = result.getData();
                if (result.getResultCode() == RESULT_OK) {
                    if (data == null || (data.getData() == null && data.getClipData() == null)) {
                        // The camera app returns no data on success — the photo
                        // is at the URI we told it to write to.
                        if (cameraOutput != null) out = new Uri[]{cameraOutput};
                    } else if (data.getClipData() != null) {
                        int n = data.getClipData().getItemCount();
                        out = new Uri[n];
                        for (int i = 0; i < n; i++) {
                            out[i] = data.getClipData().getItemAt(i).getUri();
                        }
                    } else {
                        out = new Uri[]{data.getData()};
                    }
                }
                cameraOutput = null;
                cb.onReceiveValue(out);
            });

        // Android's own camera permission, asked for only when something actually
        // wants the camera rather than on first launch — a permission dialog
        // before the app has done anything is the one people refuse.
        cameraPermissionLauncher = registerForActivityResult(
            new ActivityResultContracts.RequestPermission(), granted -> {
                PermissionRequest req = pendingPagePermission;
                pendingPagePermission = null;
                if (req == null) return;
                if (granted) req.grant(req.getResources());
                else req.deny();
            });

        scanLauncher = registerForActivityResult(new ScanContract(), result -> {
            String code = result == null ? null : result.getContents();
            if (code == null) return;                       // cancelled
            web.evaluateJavascript(
                "window.__essaScanResult(" + JSONObject.quote(code) + ")", null);
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);                       // the app keeps state in localStorage
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setMediaPlaybackRequiresUserGesture(false);

        web.addJavascriptInterface(new Bridge(), "AndroidHost");

        // A bare WebChromeClient is what made "Take a photo" and "Choose a photo"
        // do nothing at all, with no error — the page opens a file input, and a
        // WebView that has not implemented onShowFileChooser simply drops it on
        // the floor. In a browser the same page works, which is why this looked
        // like a fault in the page.
        web.setWebChromeClient(new WebChromeClient() {

            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {                 // a chooser is already open
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;
                try {
                    fileChooserLauncher.launch(buildChooser(params));
                } catch (Exception e) {
                    fileCallback = null;
                    return false;                           // let the WebView give up quietly
                }
                return true;
            }

            /**
             * The page asking for the camera itself (getUserMedia), which is how
             * the live barcode scanner and the in-app capture work.
             *
             * Chromium only offers those on a secure origin, so on the warehouse
             * LAN over plain http this never fires and the native scanner is the
             * only route. Against the deployed https server it does fire, and
             * without this it is denied by default — the page then reports a
             * camera that is present, permitted by Android, and refused by the
             * shell.
             */
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    for (String r : request.getResources()) {
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                            if (ContextCompat.checkSelfPermission(MainActivity.this,
                                    Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                                // Ask Android first; the page can ask again once
                                // the user has answered.
                                pendingPagePermission = request;
                                cameraPermissionLauncher.launch(Manifest.permission.CAMERA);
                                return;
                            }
                            request.grant(request.getResources());
                            return;
                        }
                    }
                    request.deny();
                });
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                view.evaluateJavascript(INJECTED_SCANNER_SHIM, null);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                // Only the page itself is worth interrupting for; a failed icon
                // or API poll should not throw up a dialog mid-shift.
                if (req == null || !req.isForMainFrame()) return;
                showUnreachable();
            }
        });

        web.loadUrl(base + "/m");
    }

    /**
     * The chooser the page's file input opens: the camera and the file picker
     * in one dialog.
     *
     * Both, because the page offers both and means different things by them —
     * "Take a photo" is the docket in someone's hand on the dock, "Choose a
     * photo or PDF" is the copy already on the phone or emailed over. The page's
     * `accept` attribute decides what the picker will show, so a PDF is
     * offered where the page allows one and not where it does not.
     */
    private Intent buildChooser(WebChromeClient.FileChooserParams params) {
        String[] accept = params == null ? null : params.getAcceptTypes();
        boolean wantsPdf = false;
        for (String a : accept == null ? new String[0] : accept) {
            if (a != null && a.toLowerCase().contains("pdf")) wantsPdf = true;
        }

        Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
        pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.setType(wantsPdf ? "*/*" : "image/*");
        if (wantsPdf) {
            pick.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/*", "application/pdf"});
        }
        if (params != null && params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
            pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        }

        Intent chooser = Intent.createChooser(pick, getString(R.string.pick_a_file));

        Intent camera = cameraIntent();
        if (camera != null) {
            chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{camera});
        }
        return chooser;
    }

    /**
     * The camera app, told where to put the photo.
     *
     * A FileProvider URI rather than the thumbnail the camera returns inline:
     * the inline one is a few hundred pixels wide and an invoice read from it is
     * unreadable. Null when there is no camera app to answer, in which case the
     * chooser is the picker alone rather than a dialog with a dead entry in it.
     */
    private Intent cameraIntent() {
        Intent take = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        if (take.resolveActivity(getPackageManager()) == null) return null;
        try {
            File dir = new File(getCacheDir(), "captures");
            if (!dir.exists() && !dir.mkdirs()) return null;
            File photo = File.createTempFile("essa-", ".jpg", dir);
            cameraOutput = FileProvider.getUriForFile(
                this, getPackageName() + ".fileprovider", photo);
            take.putExtra(MediaStore.EXTRA_OUTPUT, cameraOutput);
            take.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            return take;
        } catch (Exception e) {
            cameraOutput = null;
            return null;
        }
    }

    /** Exposed to the page as `AndroidHost`. */
    private class Bridge {
        @JavascriptInterface
        public void scan() {
            ScanOptions o = new ScanOptions();
            // ZXing BarcodeFormat names, spelled out rather than taken from
            // ScanOptions' constants: the same five the web app asks for in
            // SCAN_FORMATS, so the phone reads exactly what a browser would.
            o.setDesiredBarcodeFormats(
                "QR_CODE", "CODE_128", "EAN_13", "EAN_8", "CODE_39");
            o.setPrompt(getString(R.string.scan_prompt));
            o.setBeepEnabled(true);
            o.setOrientationLocked(true);
            runOnUiThread(() -> scanLauncher.launch(o));
        }
    }

    private String baseUrl() {
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        return p.getString(KEY_BASE_URL, "");
    }

    private void showUnreachable() {
        new AlertDialog.Builder(this)
            .setTitle(R.string.unreachable_title)
            .setMessage(getString(R.string.unreachable_body, baseUrl()))
            .setPositiveButton(R.string.retry, (d, w) -> web.reload())
            .setNegativeButton(R.string.change_server, (d, w) -> {
                startActivity(new Intent(this, SetupActivity.class));
                finish();
            })
            .setCancelable(false)
            .show();
    }

    /** Back walks the page's history before it leaves the app. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
