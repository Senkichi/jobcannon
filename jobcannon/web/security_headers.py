"""jobcannon/web/security_headers.py — one `after_request` hook adding
standard hardening response headers to EVERY response this app serves
(public, 401, authed, HTMX fragment alike) — issue #147.

Registered once, at the end of `jobcannon.web.create_app`, after every
blueprint is mounted and `HOST_CONFIG` / `CLERK_FRONTEND_API_HOST` are both
final, so there is exactly one place a header can be added or a CSP host
allowed — never a per-route opt-in that a new route could silently miss.

Content-Security-Policy host allow-lists are DERIVED from `HOST_CONFIG`
(the Clerk Frontend API host `jobcannon.web.clerk_frontend.frontend_api_host`
already computes for the clerk-js `<script src>` in base.html, and the
Clerk accounts host parsed out of `clerk_sign_up_url`) rather than
hardcoded literals — a key rotation or a `CLERK_SIGN_UP_URL` change takes
effect with no code edit here, the same reasoning
`jobcannon.web.clerk_frontend`'s own docstring gives for deriving the FAPI
host from the key instead of a second config var.

The policy was verified empirically, not assembled from a plausible-looking
template: a live local run of this app (TESTING config, a syntactically
valid but non-resolving `pk_test_` key so the clerk-js script tag and its
FAPI host allowance are both exercised) driven by Playwright against
/start, /privacy, /terms, /demo, asserting zero
`securitypolicyviolation`/console CSP errors — see the PR body (#147) for
the exact script path and result. `script-src`/`style-src` need
`'unsafe-inline'` (base.html's inline `<script>` blocks that call
`Clerk.load()` / `error_401.html`'s `clerk_after_load` block have no nonce
mechanism here; Tailwind's Play CDN injects a `<style>` tag with no nonce
either) and `script-src` needs `'unsafe-eval'` (Tailwind's Play CDN
JIT-compiles utility classes client-side using `new Function()` — confirmed
by the same empirical run: omitting it produced a live CSP violation on
every page, not merely a hypothesized one).

HSTS is conditional on the request having actually arrived over HTTPS,
checked via `request.is_secure` OR the `X-Forwarded-Proto` header
Render/Cloudflare set on the edge-terminated-TLS-but-forwarded-as-HTTP
request reaching this app (there is no `ProxyFix` anywhere in this app, so
`request.is_secure` alone stays False for every production request even
though the browser's own connection is HTTPS) — never emitted for a
genuinely plain-HTTP request, so it can never brick an operator's own
http://localhost dev run.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Flask, Response, request

_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"

# Static regardless of HOST_CONFIG — never conditional, never opted out of
# per route (issue #147's whole point: no per-route gap).
_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": _PERMISSIONS_POLICY,
}

_HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _is_https(req) -> bool:
    """True if THIS request reached the app over HTTPS, accounting for the
    Render/Cloudflare edge that terminates TLS and forwards plain HTTP to
    the app process (no ProxyFix is installed, so `req.is_secure` alone
    would stay False for every production request otherwise). Only the
    first, edge-set hop of a comma-separated X-Forwarded-Proto is trusted —
    this app sits directly behind exactly one trusted proxy layer, not a
    chain a client could prepend to."""
    if req.is_secure:
        return True
    proto = req.headers.get("X-Forwarded-Proto", "")
    return proto.split(",")[0].strip().lower() == "https"


def _accounts_host(clerk_sign_up_url: str) -> str | None:
    """Hostname Clerk's hosted Account Portal sign-up/sign-in flow lives on,
    parsed out of `clerk_sign_up_url` (error_401.html's own sign-in/sign-up
    link) rather than a second hardcoded literal. Returns None when unset
    (local dev, most TESTING doubles) or unparseable, so the CSP builder
    below can omit `form-action`'s extra host entirely instead of emitting
    a malformed policy fragment."""
    if not clerk_sign_up_url:
        return None
    try:
        host = urlsplit(clerk_sign_up_url).hostname
    except ValueError:
        return None
    return host or None


def _build_csp(*, frontend_api_host: str, accounts_host: str | None) -> str:
    script_hosts = ["https://cdn.tailwindcss.com", "https://unpkg.com"]
    connect_hosts = ["'self'"]
    if frontend_api_host:
        script_hosts.append(f"https://{frontend_api_host}")
        connect_hosts.append(f"https://{frontend_api_host}")

    form_action = ["'self'"]
    if accounts_host:
        form_action.append(f"https://{accounts_host}")

    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'", *script_hosts, "'unsafe-inline'", "'unsafe-eval'"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "img-src": ["'self'", "data:"],
        "font-src": ["'self'"],
        "connect-src": connect_hosts,
        "frame-src": ["'none'"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'self'"],
        "object-src": ["'none'"],
        "form-action": form_action,
    }
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


def register_security_headers(app: Flask) -> None:
    """Precompute the (request-independent) CSP string once from
    `HOST_CONFIG` at registration time — not on every request — then apply
    it, the other static headers, and the request-conditional HSTS header
    inside one `after_request` hook."""
    host_config = app.config["HOST_CONFIG"]
    frontend_api_host = app.config.get("CLERK_FRONTEND_API_HOST", "") or ""
    accounts_host = _accounts_host(getattr(host_config, "clerk_sign_up_url", "") or "")
    csp = _build_csp(frontend_api_host=frontend_api_host, accounts_host=accounts_host)

    @app.after_request
    def _add_security_headers(response: Response) -> Response:
        for name, value in _STATIC_HEADERS.items():
            response.headers[name] = value
        response.headers["Content-Security-Policy"] = csp
        if _is_https(request):
            response.headers["Strict-Transport-Security"] = _HSTS_VALUE
        return response
