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


# Every (concrete path, method) request the gate is allowed to stand down
# for, paired with the REAL status a signed-out visitor gets there -- an
# explicit, dissent-able table (refuter-3 F1) rather than the previous
# `!= 401` catch-all, which would have silently gone toothless (accepting
# 404/500/302 alongside a real 200) the moment a second public_get view
# existed. Keyed by (path, method) rather than (endpoint, method): /consent
# registers TWO separate Rule objects (@consent_bp.get + @consent_bp.post),
# both auto-declaring OPTIONS, but there is only ONE real "OPTIONS
# /consent" request an actual client can send -- Werkzeug resolves it to a
# single rule internally, so the table (and the loop below) has to key on
# the request that's actually made, not on which Rule object happened to
# declare it. Today this is exactly GET/HEAD/OPTIONS /consent (issue
# #171); adding an entry here without ANY matching rule's view being
# `@public_get`-marked fails the cross-check loop below, and marking a
# view `@public_get` without adding its entries here fails the main loop
# instead (the unmarked-request `== 401` branch catches the mismatch).
_EXEMPT_STATUS = {
    ("/consent", "GET"): 200,
    ("/consent", "HEAD"): 200,
    ("/consent", "OPTIONS"): 200,
}


def test_gate_covers_every_registered_route_for_every_declared_method():
    """The invariant #173's fix must not weaken: every REAL, matched,
    non-public route still 401s a signed-out visitor, for every method it
    declares -- including OPTIONS, which used to be excluded from this
    loop entirely (refuter-1 + refuter-3 F2: a probe confirms
    OPTIONS /postings/1/save->401 same as any other method, so there was
    no reason to skip it) -- derived from app.url_map.iter_rules() itself
    (never a hand-maintained path list), so a future route is automatically
    covered by this test too. Two exemptions, both intentional, none
    hand-picked by route name: PUBLIC_PATHS members (clerk_auth's own
    public-path branch) and _EXEMPT_STATUS above (issue #171's
    public_get-marked GET/HEAD/OPTIONS /consent, asserting the real
    status a signed-out visitor gets, not merely "not 401")."""
    app = _app()
    client = app.test_client()

    # Group by (concrete path, method), collecting every endpoint that
    # declares each one (a list, not a single winner -- see _EXEMPT_STATUS's
    # comment on why /consent's OPTIONS is ambiguous across two Rules).
    endpoints_by_request: dict[tuple[str, str], list[str]] = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("webhooks."):
            continue
        path = _concrete_path(rule.rule)
        normalized = path.rstrip("/") or "/"
        if normalized in PUBLIC_PATHS:
            continue
        for method in rule.methods:
            endpoints_by_request.setdefault((path, method), []).append(rule.endpoint)

    checked = 0
    for (path, method), endpoints in sorted(endpoints_by_request.items()):
        resp = client.open(path, method=method)
        if (path, method) in _EXEMPT_STATUS:
            assert resp.status_code == _EXEMPT_STATUS[(path, method)], (
                f"{method} {path} (endpoints={endpoints}) -> {resp.status_code}, "
                f"expected {_EXEMPT_STATUS[(path, method)]} (declared exemption)"
            )
        else:
            assert resp.status_code == 401, (
                f"{method} {path} (endpoints={endpoints}) -> {resp.status_code}, expected 401"
            )
        checked += 1

    # _EXEMPT_STATUS can't silently drift out of sync with the real
    # predicate the gate consults: every declared exemption must have AT
    # LEAST ONE rule at that (path, method) whose view
    # _is_auth_optional_for_method -- the SAME function clerk_auth
    # consults -- agrees is exempt.
    for path, method in _EXEMPT_STATUS:
        endpoints = endpoints_by_request.get((path, method), [])
        assert any(
            _is_auth_optional_for_method(app.view_functions.get(ep), method) for ep in endpoints
        ), (
            f"{method} {path} is in _EXEMPT_STATUS but no matching rule's view "
            f"agrees it's exempt -- table drifted from the gate"
        )

    # Sanity floor so a future refactor that accidentally empties the
    # iteration (e.g. an app.url_map that failed to register blueprints)
    # can't silently pass with zero assertions actually run.
    assert checked >= 10


def test_wrong_method_on_a_public_path_still_405s_not_silently_exempted():
    """Load-bearing design point with no prior test (devin + refuter-3 F3):
    clerk_auth's routing_exception re-raise runs BEFORE the PUBLIC_PATHS
    check (__init__.py, deliberately unconditional per that function's own
    comment), so a wrong-method request against a PUBLIC path must still
    405, not silently pass through as "public" and fail deeper in the
    stack or get treated as a 200. Two different PUBLIC_PATHS routes
    (POST /privacy -- GET-only; PUT /healthz -- GET-only) so this isn't
    pinned to one route's registration quirk."""
    app = _app()
    client = app.test_client()

    resp = client.post("/privacy")
    assert resp.status_code == 405
    assert "Allow" in resp.headers

    resp2 = client.put("/healthz")
    assert resp2.status_code == 405
    assert "Allow" in resp2.headers


def test_signed_in_visitor_hitting_a_bogus_path_still_gets_a_404_page():
    """devin MED + refuter-3 F4: the routing_exception re-raise fires
    before g.clerk_user is ever assigned, so without an explicit `None`
    set on this path, a template read of g.clerk_user (base.html's header
    nav) would see it entirely unset for a SIGNED-IN visitor hitting an
    unmatched path -- unlike the 401 path, which always sets it first.
    Proves the page still renders (a crash would surface as 500, same
    verification test_two_bogus_paths_return_404_not_401 relies on) and
    documents the accepted trade-off explicitly: g.clerk_user is set to
    None on this path (mirroring the public-path branch), so a signed-in
    visitor sees the SIGNED-OUT header nav on this one page -- resolving
    real identity here would mean running VERIFY_REQUEST speculatively on
    every unmatched-path probe, and the 404 status must never depend on
    it."""
    from jobcannon.web.auth import ClerkIdentity

    app = _app(verify=lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}))
    resp = app.test_client().get("/does-not-exist")

    assert resp.status_code == 404
    html = resp.get_data(as_text=True)
    assert "data-auth-nav" in html  # signed-out nav renders even though this visitor is signed in
