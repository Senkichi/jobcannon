"""jobcannon/web/__init__.py's routing-error handling (issue #173): an
unmatched path or a wrong HTTP method on a real route must reach Flask's
own 404/405 errorhandlers, not the 401 auth gate -- and the invariant that
motivated the fix (every REAL, non-public route still 401s a signed-out
visitor) has to keep holding for every route the app registers, not just
the ones a hand-picked list happens to cover.

No Postgres needed: every route exercised here either hits an
errorhandler (no DB) or aborts in clerk_auth before any view runs, same
shape as tests/host/test_auth.py and tests/host/test_auth_nav.py.
"""

from __future__ import annotations

import re

from jobcannon.host.config import HostConfig
from jobcannon.web import PUBLIC_PATHS, _is_auth_optional_for_method, create_app

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="

# The only two Werkzeug converters this app's routes actually use today
# (confirmed against app.url_map.iter_rules()) -- a placeholder value per
# converter kind, substituted into each rule's raw path template so the
# invariant test below can request a REAL, concrete URL for every
# registered rule without a hand-maintained per-route path list.
_CONVERTER_PLACEHOLDERS = {"int": "1", "path": "probe-asset.txt"}
_CONVERTER_RE = re.compile(r"<(?:([a-zA-Z_]+):)?[a-zA-Z_]\w*>")


def _concrete_path(rule_template: str) -> str:
    def _sub(match: re.Match) -> str:
        converter = match.group(1) or "string"
        return _CONVERTER_PLACEHOLDERS.get(converter, "x")

    return _CONVERTER_RE.sub(_sub, rule_template)


def _app(verify=None, host_config: HostConfig | None = None):
    return create_app(
        config={
            "TESTING": True,
            "HOST_CONFIG": host_config
            or HostConfig(database_url="", secret_key="testing-secret-key"),
            "VERIFY_REQUEST": verify or (lambda req: None),
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def test_two_bogus_paths_return_404_not_401():
    app = _app()
    client = app.test_client()

    for bogus in ("/does-not-exist", "/zzz-totally-bogus-path-12345"):
        resp = client.get(bogus)
        assert resp.status_code == 404, bogus
        assert b"Sign-in required" not in resp.data


def test_wrong_method_on_real_route_returns_405_with_allow_header():
    # /postings/<id>/save only registers POST (jobcannon/web/actions.py) --
    # issue #173's own repro example.
    app = _app()
    client = app.test_client()

    resp = client.put("/postings/1/save")

    assert resp.status_code == 405
    assert "Allow" in resp.headers
    assert "POST" in resp.headers["Allow"]
    assert b"Sign-in required" not in resp.data


def test_404_and_405_are_branded_html_pages_for_a_direct_browser_request():
    app = _app()
    client = app.test_client()

    resp = client.get("/does-not-exist")
    html = resp.get_data(as_text=True)

    assert "<html" in html
    assert "Page not found" in html


def test_404_hx_request_gets_a_small_non_html_fragment_not_the_full_page():
    """htmx 2 does not swap a non-2xx response into the DOM by default
    (htmx.config.responseHandling marks 4xx as swap: false), so the real
    point of this branch is never sending a full <html> document as an
    XHR body -- verified the same way test_401_page's HX branch would be:
    body must never contain '<html'."""
    app = _app()
    client = app.test_client()

    resp = client.get("/does-not-exist", headers={"HX-Request": "true"})

    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert "<html" not in body
    assert "doesn't exist" in body


def test_405_hx_request_gets_a_small_fragment_and_keeps_allow_header():
    app = _app()
    client = app.test_client()

    resp = client.put("/postings/1/save", headers={"HX-Request": "true"})

    assert resp.status_code == 405
    assert "Allow" in resp.headers
    body = resp.get_data(as_text=True)
    assert "<html" not in body


def test_bad_request_errorhandler_renders_branded_page():
    """Covers a Werkzeug BadRequest raised from inside this app (see
    bad_request's docstring for the scope limit: a front-proxy
    request-line rejection never reaches Flask at all, so this only
    proves the in-app path). Signed in so the request reaches the view
    (and the BadRequest it raises) instead of being rejected by the auth
    gate first -- this test is about the errorhandler, not about auth."""
    from jobcannon.web.auth import ClerkIdentity

    app = _app(verify=lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}))

    @app.get("/__raise-bad-request-for-test")
    def _raise_bad_request():
        from werkzeug.exceptions import BadRequest

        raise BadRequest()

    resp = app.test_client().get("/__raise-bad-request-for-test")

    assert resp.status_code == 400
    assert "Bad request" in resp.get_data(as_text=True)


def test_static_path_is_unaffected_still_401s_signed_out():
    """Design invariant: /static/<path> matches a real Werkzeug rule
    (registered unconditionally, not in PUBLIC_PATHS) regardless of
    whether the file exists, so routing_exception is None and the request
    still reaches -- and is still rejected by -- the ordinary auth gate.
    A missing file 404s only from inside the view, which a signed-out
    visitor never reaches."""
    app = _app()
    resp = app.test_client().get("/static/does-not-exist.css")

    assert resp.status_code == 401


def test_healthz_is_unaffected_by_the_routing_exception_check():
    app = _app()
    resp = app.test_client().get("/healthz")

    assert resp.status_code == 200


def test_gate_covers_every_registered_route_for_every_declared_method():
    """The invariant #173's fix must not weaken: every REAL, matched,
    non-public route still 401s a signed-out visitor, for every method it
    declares -- derived from app.url_map.iter_rules() itself (never a
    hand-maintained path list), so a future route is automatically
    covered by this test too. Three exemptions, all intentional and
    pre-existing or from issue #171, none hand-picked by route name:
    PUBLIC_PATHS members (clerk_auth's own public-path branch), the
    `webhooks` blueprint (signature-verified instead of session-gated --
    tests/host/test_auth.py already covers its own behavior in
    isolation), and a method `_is_auth_optional_for_method` (public_get's
    marker predicate, the SAME one clerk_auth itself consults) says is
    exempt for that view -- e.g. GET/HEAD /consent, issue #171 --
    verified with a `!= 401` assertion (proving the gate stood down)
    rather than a hardcoded expected status, since a marked view's
    signed-out response shape is that view's own choice, not this
    gate-coverage test's concern."""
    app = _app()
    client = app.test_client()

    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("webhooks."):
            continue
        path = _concrete_path(rule.rule)
        normalized = path.rstrip("/") or "/"
        if normalized in PUBLIC_PATHS:
            continue
        view_func = app.view_functions.get(rule.endpoint)
        for method in sorted(rule.methods - {"OPTIONS"}):
            resp = client.open(path, method=method)
            if _is_auth_optional_for_method(view_func, method):
                assert resp.status_code != 401, (
                    f"{method} {path} (endpoint={rule.endpoint}) is public_get-marked "
                    f"but still 401'd signed out"
                )
            else:
                assert resp.status_code == 401, (
                    f"{method} {path} (endpoint={rule.endpoint}) -> {resp.status_code}, expected 401"
                )
            checked += 1

    # Sanity floor so a future refactor that accidentally empties the
    # iteration (e.g. an app.url_map that failed to register blueprints)
    # can't silently pass with zero assertions actually run.
    assert checked >= 10
