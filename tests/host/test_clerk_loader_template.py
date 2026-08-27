"""Issue #149 design C: base.html and error_401.html must emit Clerk's
standard clerk-js loader (with the configured publishable key + the
derived FAPI host) when a publishable key is configured, and must NOT emit
it when blank (TESTING's default HostConfig). error_401.html additionally
carries the reload-once-on-live-session guard clerk-js's cross-domain
handshake needs to actually repair the #149 401 loop.

Issue #158 narrows this further: even with a publishable key configured,
the loader must be ABSENT on every PUBLIC_PATHS page (/demo, /start,
/preview, /privacy, /terms) — those are reachable before a visitor has any
reason to contact Clerk, so loading clerk-js there means Clerk (and
Cloudflare in front of it) sees the visitor's IP/UA and sets cookies before
they've done anything. It must still load on the 401 page (the #151
handshake target) and on pages rendered for a signed-in visitor — both
covered by the tests above this comment, which double as the positive
control proving the assertion mechanism can actually detect the loader.

No Postgres needed: these hit either the errorhandler (no DB), the /privacy
route (also no DB — jobcannon.web.legal, see tests/host/test_legal_pages.py),
or a custom throwaway route registered the same way test_auth.py's
`/private` tests do."""

from jobcannon.host.config import HostConfig
from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity

# base64("example.com$") == "ZXhhbXBsZS5jb20k" -> FAPI host "example.com".
_TEST_PUBLISHABLE_KEY = "pk_test_ZXhhbXBsZS5jb20k"
_TEST_FAPI_HOST = "example.com"
_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def _host_config(**overrides) -> HostConfig:
    fields = dict(database_url="", secret_key="testing-secret-key")
    fields.update(overrides)
    return HostConfig(**fields)


def _app(host_config: HostConfig, verify):
    return create_app(
        config={
            "TESTING": True,
            "HOST_CONFIG": host_config,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def test_authed_page_loads_clerk_js_when_publishable_key_configured():
    app = _app(
        _host_config(clerk_publishable_key=_TEST_PUBLISHABLE_KEY),
        verify=lambda req: ClerkIdentity(user_id="user_123", claims={"sub": "user_123"}),
    )

    @app.get("/render-base")
    def render_base():
        from flask import render_template

        return render_template("base.html")

    html = app.test_client().get("/render-base").get_data(as_text=True)
    assert f'data-clerk-publishable-key="{_TEST_PUBLISHABLE_KEY}"' in html
    assert f'src="https://{_TEST_FAPI_HOST}/npm/@clerk/clerk-js@5/dist/clerk.browser.js"' in html
    assert "await window.Clerk.load()" in html


def test_authed_page_omits_clerk_js_when_publishable_key_blank():
    """TESTING's default HostConfig leaves clerk_publishable_key blank —
    the loader must not render at all (not an empty src, absent entirely),
    since most tests/dev setups never configure a real Clerk key."""
    app = _app(
        _host_config(),  # clerk_publishable_key defaults to ""
        verify=lambda req: ClerkIdentity(user_id="user_123", claims={"sub": "user_123"}),
    )

    @app.get("/render-base")
    def render_base():
        from flask import render_template

        return render_template("base.html")

    html = app.test_client().get("/render-base").get_data(as_text=True)
    assert "clerk-publishable-key" not in html
    assert "clerk.browser.js" not in html


def test_401_page_loads_clerk_js_when_publishable_key_configured():
    """error_401.html is the page a human actually lands on after the
    hosted Account Portal redirect (issue #149) — this is the one that
    matters most for the fix to actually work."""
    app = _app(
        _host_config(clerk_publishable_key=_TEST_PUBLISHABLE_KEY),
        verify=lambda req: None,
    )

    html = app.test_client().get("/").get_data(as_text=True)
    assert f'data-clerk-publishable-key="{_TEST_PUBLISHABLE_KEY}"' in html
    assert f'src="https://{_TEST_FAPI_HOST}/npm/@clerk/clerk-js@5/dist/clerk.browser.js"' in html


def test_401_page_omits_clerk_js_when_publishable_key_blank():
    app = _app(_host_config(), verify=lambda req: None)

    html = app.test_client().get("/").get_data(as_text=True)
    assert "clerk-publishable-key" not in html
    assert "clerk.browser.js" not in html


def test_401_page_carries_the_reload_once_guard():
    """Proves the 401-page-specific clerk_after_load override actually
    lands in the rendered output (not just the shared base.html script) —
    the sessionStorage guard that stops an azp/config mistake from
    producing an infinite reload loop."""
    app = _app(
        _host_config(clerk_publishable_key=_TEST_PUBLISHABLE_KEY),
        verify=lambda req: None,
    )

    html = app.test_client().get("/").get_data(as_text=True)
    assert "window.Clerk.session" in html
    assert "sessionStorage" in html
    assert "jc_clerk_401_reload_at" in html
    assert "window.location.reload()" in html


def test_privacy_and_terms_omit_clerk_js_even_when_publishable_key_configured():
    """The core #158 regression: a configured publishable key alone used to
    be sufficient to load clerk-js everywhere (this module's earlier
    positive-control tests). It must no longer be sufficient on a
    PUBLIC_PATHS page — /privacy and /terms are the two DB-free ones,
    exercised through their real routes (jobcannon.web.legal), not a
    synthetic test route, so this pins the actual production code path."""
    app = _app(
        _host_config(clerk_publishable_key=_TEST_PUBLISHABLE_KEY),
        verify=lambda req: None,
    )
    client = app.test_client()

    for path in ("/privacy", "/terms"):
        resp = client.get(path)
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "clerk-publishable-key" not in html
        assert "clerk.browser.js" not in html
        assert "Clerk.load" not in html


def test_is_public_request_path_matches_every_public_path():
    """Direct unit coverage of the shared normalization both clerk_auth's
    before_request gate and inject_clerk_frontend's clerk-js gate rely on —
    exercised against every PUBLIC_PATHS entry via a real request context
    rather than only the two DB-free routes above, so /demo, /start, and
    /preview (which need Postgres to render end-to-end, per
    tests/host/test_demo_feed.py and tests/host/test_preview.py) are still
    covered for the gating decision itself."""
    from jobcannon.web import PUBLIC_PATHS, _is_public_request_path

    app = _app(_host_config(), verify=lambda req: None)
    for path in PUBLIC_PATHS:
        with app.test_request_context(path):
            assert _is_public_request_path() is True, path


def test_is_public_request_path_false_for_non_public_routes():
    """Negative control: a path that is NOT in PUBLIC_PATHS — including one
    that doesn't correspond to any registered route at all, the same
    unmatched-path case the 401 handler's docstring calls out — must not be
    treated as public."""
    from jobcannon.web import _is_public_request_path

    app = _app(_host_config(), verify=lambda req: None)
    for path in ("/", "/account/export", "/consent", "/render-base", "/no-such-route"):
        with app.test_request_context(path):
            assert _is_public_request_path() is False, path


def test_is_public_request_path_normalizes_trailing_slash():
    """/demo is registered strict_slashes=False, so /demo/ must match too —
    mirrors tests/host/test_pages.py's
    test_demo_trailing_slash_is_public_not_401 regression for the auth gate,
    pinned here for the clerk-js gate's shared normalization specifically."""
    from jobcannon.web import _is_public_request_path

    app = _app(_host_config(), verify=lambda req: None)
    with app.test_request_context("/demo/"):
        assert _is_public_request_path() is True


def test_authed_base_page_does_not_carry_the_401_reload_guard():
    """The reload-once guard is 401-page-specific — a normal authed page
    (which never redirected through the Account Portal) must not carry
    it, only the shared Clerk.load() call."""
    app = _app(
        _host_config(clerk_publishable_key=_TEST_PUBLISHABLE_KEY),
        verify=lambda req: ClerkIdentity(user_id="user_123", claims={"sub": "user_123"}),
    )

    @app.get("/render-base")
    def render_base():
        from flask import render_template

        return render_template("base.html")

    html = app.test_client().get("/render-base").get_data(as_text=True)
    assert "jc_clerk_401_reload_at" not in html
    assert "window.location.reload()" not in html
