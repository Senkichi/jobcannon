"""jobcannon.web.security_headers coverage — issue #147.

Four response shapes, matching the issue's own test list: a public page
(no auth), the 401 page (VERIFY_REQUEST denies), an authed page, and an
HTMX fragment (a real `_posting_row.html` render, not just the CSRF
fragment tests/host/test_csrf.py already covers). All four go through the
SAME `register_security_headers`'s `after_request` hook — jobcannon's
whole point (#147) is that there is exactly one place a header can be
added, never a per-route opt-in a new route could miss — so this module
does not re-test every route in the app; it tests the shapes, on ONE route
each, that a route-scoped regression (someone adding a second, competing
`after_request` that only fires for some blueprints) would be able to slip
past.

`/privacy` and `/account/delete` (GET) need no live Postgres — neither
route touches the DB (jobcannon/web/legal.py's markdown is rendered once at
import time; jobcannon/web/account.py's GET handler is a static form
render, per that module's own docstring). The HTMX-fragment case needs a
real posting row, so it opens its own throwaway database, the same pattern
tests/host/test_csrf.py's `db_app` fixture uses.
"""

from __future__ import annotations

import base64

import psycopg
import pytest

from jobcannon.host.config import HostConfig
from jobcannon.web.auth import ClerkIdentity
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

USER_ID = "user_headers_test"
WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="

# A syntactically valid, non-resolving key -- base64("clerk.example.test$")
# -- so the CSP's script-src/connect-src derivation (jobcannon.web.
# security_headers._build_csp) actually has a frontend_api_host to append,
# the same shape the module's own docstring documents verifying empirically
# against a live Playwright run.
_PK = "pk_test_" + base64.b64encode(b"clerk.example.test$").decode()
_ACCOUNTS_HOST = "accounts.example.test"

_REQUIRED_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _host_config(**overrides) -> HostConfig:
    fields = dict(
        database_url="",
        secret_key="headers-test-secret",
        clerk_sign_up_url=f"https://{_ACCOUNTS_HOST}/sign-up",
        signup_wave="0",
        clerk_publishable_key=_PK,
    )
    fields.update(overrides)
    return HostConfig(**fields)


def _app(**config_overrides):
    from jobcannon.web import create_app

    config = {
        "TESTING": True,
        "HOST_CONFIG": _host_config(),
        "WEBHOOK_SECRET": WEBHOOK_SECRET,
    }
    config.update(config_overrides)
    return create_app(config=config)


def _assert_static_headers(headers) -> None:
    for name, value in _REQUIRED_STATIC_HEADERS.items():
        assert headers.get(name) == value, f"{name} missing or wrong: {headers.get(name)!r}"


def _assert_csp(headers) -> str:
    csp = headers.get("Content-Security-Policy")
    assert csp, "Content-Security-Policy header missing"
    for directive in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-src 'none'",
    ):
        assert directive in csp, f"missing CSP directive {directive!r} in {csp!r}"
    # Derived from HOST_CONFIG, not hardcoded (issue #147's design
    # requirement) -- both the FAPI host (from the publishable key) and the
    # accounts host (from clerk_sign_up_url) must show up verbatim.
    assert "https://clerk.example.test" in csp
    assert f"https://{_ACCOUNTS_HOST}" in csp
    assert "form-action 'self'" in csp
    return csp


# ---------------------------------------------------------------------------
# Public page: GET /privacy, no auth, no DB.
# ---------------------------------------------------------------------------


def test_public_page_carries_security_headers():
    client = _app().test_client()
    resp = client.get("/privacy")
    assert resp.status_code == 200
    _assert_static_headers(resp.headers)
    _assert_csp(resp.headers)


def test_public_page_has_no_hsts_over_plain_http():
    """The dev server / test client's request is plain HTTP (no
    X-Forwarded-Proto, request.is_secure False) -- HSTS must never fire for
    that, or an operator's own http://localhost run gets bricked into
    HTTPS-only by their own browser (jobcannon.web.security_headers's
    _is_https docstring)."""
    client = _app().test_client()
    resp = client.get("/privacy")
    assert "Strict-Transport-Security" not in resp.headers


def test_public_page_has_hsts_behind_the_render_cloudflare_edge():
    """Render/Cloudflare terminates TLS and forwards plain HTTP with
    X-Forwarded-Proto: https -- the one condition production HSTS actually
    fires under, since this app installs no ProxyFix (request.is_secure
    alone stays False for every real production request)."""
    client = _app().test_client()
    resp = client.get("/privacy", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"


# ---------------------------------------------------------------------------
# The 401 page: VERIFY_REQUEST denies, error_401.html renders.
# ---------------------------------------------------------------------------


def test_401_page_carries_security_headers():
    client = _app(VERIFY_REQUEST=lambda req: None).test_client()
    resp = client.get("/")  # "/" is authed-only, not in PUBLIC_PATHS
    assert resp.status_code == 401
    _assert_static_headers(resp.headers)
    _assert_csp(resp.headers)


# ---------------------------------------------------------------------------
# An authed page: GET /account/delete, no DB.
# ---------------------------------------------------------------------------


def test_authed_page_carries_security_headers():
    client = _app(
        VERIFY_REQUEST=lambda req: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID})
    ).test_client()
    resp = client.get("/account/delete")
    assert resp.status_code == 200
    _assert_static_headers(resp.headers)
    _assert_csp(resp.headers)


# ---------------------------------------------------------------------------
# An HTMX fragment: POST /postings/<id>/save, real posting, real DB.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_headers_test")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = _app(
            VERIFY_REQUEST=lambda req: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID})
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


@requires_postgres
def test_htmx_fragment_carries_security_headers(db_app):
    dsn = db_app.config["_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s)", (USER_ID,))
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES ('Headers Test Co') RETURNING id"
        ).fetchone()[0]
        posting_id = conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES ('headers-test-1', %s, 'Engineer', 'Headers Test Co') RETURNING id",
            (company_id,),
        ).fetchone()[0]
        conn.commit()

    client = db_app.test_client()
    # Skip the handoff redirect (jobcannon.web.handoff.run_handoff_if_pending
    # would otherwise 302 this authed request to /consent on its first hit,
    # same reason tests/host/test_csrf.py's db_app fixture sets this).
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    resp = client.post(f"/postings/{posting_id}/save", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"<html" not in resp.data  # confirms this really is a fragment, not a full page
    _assert_static_headers(resp.headers)
    _assert_csp(resp.headers)
