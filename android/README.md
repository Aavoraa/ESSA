# ESSA AI — Android app

The phone app is the warehouse's own `/m` screens in a WebView, with scanning done
natively. It is a shell, not a copy: the pages come from the server every time, so
changing the mobile UI needs no new APK.

## Getting an APK

**On GitHub — nothing to install.** Push this folder, then open **Actions → Build
warehouse APK → Run workflow**. When it finishes, the APK is under **Artifacts**
on that run. Download it on the phone and open it; Android will ask to allow
installing from this source.

**Locally**, if you have Android Studio: open `android/` and Run, or

    cd android
    gradle assembleDebug        # or ./gradlew assembleDebug once Studio makes the wrapper
    # app/build/outputs/apk/debug/app-debug.apk

The build is **debug only**, on purpose: a debug APK is signed with the standard
debug key, which is all a side-loaded internal app needs, and a release key does
not belong in a repository. It will not go on Play Store as-is.

## First run

The app asks for the warehouse address — the LAN address of the PC running the
server, e.g. `192.168.1.5`. Port `8000` is assumed if you leave it off. It is
checked against `/api/status` before being saved, and remembered after that. If
the server later moves, the "not reachable" dialog offers **Change server**.

The phone must be on the same Wi‑Fi as that PC, and the server has to be listening
on the network rather than only on localhost — `--host 0.0.0.0`, which is how the
project already starts it.

## Why scanning is native

The web app scans with `navigator.mediaDevices.getUserMedia` and `BarcodeDetector`.
Chromium only exposes those on a **secure origin**, and `http://192.168.1.5:8000`
is not one — in a WebView `mediaDevices` is simply undefined and the camera button
does nothing. Working around that means either serving the warehouse over HTTPS
with a self-signed certificate the app is taught to accept, or bundling the pages
into the APK and rebuilding it whenever the UI changes. Both cost more than they
give here.

So the page is left alone and, once loaded, `MainActivity.INJECTED_SCANNER_SHIM`
replaces its `openScanner` with one that opens a native ZXing scanner and hands
the decoded string back. Both ways the page scans — with a callback and without —
behave exactly as they do in a browser.

That shim reaches into the page's own globals (`openScanner`, `resolveCode`, `S`,
`render`, `toast`), which is not an interface anyone agreed to. Rename one in
`backend/app/mobile/index.html` and the scan button quietly stops working. Hence:

    node android/tools/shim_test.mjs

which extracts the shim from `MainActivity.java`, runs it against stand-ins, and
first checks those globals are still in the page. CI runs it before every build.

## Cleartext HTTP

`res/xml/network_security_config.xml` permits it. Android has blocked cleartext by
default since Android 9, and the warehouse is a plain-HTTP LAN server. It is
allowed for all hosts rather than for private ranges only because that file matches
hostnames and IP literals — it has no CIDR syntax, so "the local network" cannot be
expressed. If this is ever exposed beyond the shop's own network, give the server a
certificate and delete that file: the app will then refuse cleartext by itself.
