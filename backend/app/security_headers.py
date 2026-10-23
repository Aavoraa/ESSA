"""
The response headers every page and every API answer goes out with.

These are also set at the nginx layer (deploy/nginx/essa.conf) — on purpose, as
two locks rather than one: the app may be run without that proxy in front (a
warehouse laptop, the Docker image on its own), and a header that only nginx
adds is a header that silently disappears the day someone deploys without it.
nginx's `add_header` and these do not stack into duplicates: the config there
hides the upstream copies with `proxy_hide_header` before adding its own.

The Content-Security-Policy differs by what is being served, because what the
three front-ends are made of differs:

    the desktop app (/, /assets)   a Vite bundle — no inline script at all, so
                                   `script-src 'self'` with nothing relaxed.
    the phone app (/m)             one hand-written HTML file whose buttons are
    the shop (/pos)                inline `onclick=` handlers, and the shop pulls
                                   Bootstrap and Chart.js from a CDN. Both need
                                   'unsafe-inline' for scripts until they are
                                   rewritten; everything else is still locked
                                   down (no foreign frames, no plugins, no
                                   foreign form targets, no foreign framing).
    /docs, /redoc                  FastAPI's API browser loads from a CDN.
"""

FONTS_CSS = "https://fonts.googleapis.com"
FONTS = "https://fonts.gstatic.com"
CDNS = "https://cdn.jsdelivr.net https://unpkg.com"

_COMMON = ("base-uri 'self'; object-src 'none'; form-action 'self'; "
           # Framed only by this site itself: the desktop app shows the shop and
           # invoice scans in iframes of its own origin. Anything else framing it
           # is clickjacking.
           "frame-ancestors 'self'")

CSP_APP = ("default-src 'self'; script-src 'self'; "
           f"style-src 'self' 'unsafe-inline' {FONTS_CSS}; font-src 'self' {FONTS} data:; "
           "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
           "frame-src 'self' blob:; worker-src 'self' blob:; " + _COMMON)

CSP_LEGACY = ("default-src 'self'; "
              f"script-src 'self' 'unsafe-inline' {CDNS}; "
              f"style-src 'self' 'unsafe-inline' {FONTS_CSS} {CDNS}; "
              f"font-src 'self' {FONTS} {CDNS} data:; "
              "img-src 'self' data: blob:; media-src 'self' blob: mediastream:; "
              "connect-src 'self'; frame-src 'self' blob:; worker-src 'self' blob:; "
              + _COMMON)

CSP_DOCS = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "connect-src 'self'; worker-src 'self' blob:; " + _COMMON)

# The camera scans barcodes and the microphone takes voice entry, on this
# site's own pages only. Nothing here has any use for the rest.
PERMISSIONS = ("camera=(self), microphone=(self), geolocation=(), payment=(), usb=(), "
               "serial=(), bluetooth=(), magnetometer=(), gyroscope=(), accelerometer=(), "
               "interest-cohort=()")

HSTS = "max-age=31536000; includeSubDomains"


def csp_for(path: str) -> str:
    if path == "/m" or path.startswith("/m/") or path == "/pos" or path.startswith("/pos/"):
        return CSP_LEGACY
    if path.startswith("/docs") or path.startswith("/redoc"):
        return CSP_DOCS
    return CSP_APP


def _is_https(scope) -> bool:
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-proto":
            return v.decode("latin-1").split(",")[0].strip() == "https"
    return scope.get("scheme") == "https"


class SecurityHeadersMiddleware:
    """Pure ASGI rather than BaseHTTPMiddleware, so streamed answers (a large
    report, a CSV export) are not buffered whole just to have headers added."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        https = _is_https(scope)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                drop = {b"server", b"x-powered-by", b"content-security-policy",
                        b"x-frame-options", b"x-content-type-options",
                        b"referrer-policy", b"permissions-policy",
                        b"strict-transport-security"}
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in drop]
                headers += [
                    (b"content-security-policy", csp_for(path).encode()),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", PERMISSIONS.encode()),
                    (b"cross-origin-opener-policy", b"same-origin"),
                ]
                # Only over HTTPS: sent over plain http on a LAN laptop it would
                # be ignored at best, and at worst pin a hostname to https that
                # has no certificate.
                if https:
                    headers.append((b"strict-transport-security", HSTS.encode()))
                message = dict(message, headers=headers)
            await send(message)

        return await self.app(scope, receive, send_wrapper)
