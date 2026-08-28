"""jobcannon/web/__init__.py's `_vary_and_cache_public_paths` after_request
hook (issue #205 fallout, review-1 MED-1 / Devin MED-1+LOW-1).

Every PUBLIC_PATHS response now varies by real visitor identity (the nav/
footer), even though each route's own body stays identity-independent — see
jobcannon/web/legal.py's Cache-Control comment for the full reasoning. The
hook forecloses cross-visitor shared-cache leakage two ways: `Vary: Cookie`
(appended, never overwriting) and `Cache-Control: private` (set only when a
route hasn't already set one, so /privacy and /terms's pre-existing
`private, max-age=300` from jobcannon/web/legal.py's `_legal_response` is
left completely untouched).

Correction to the original Devin MED finding this hook implements: `Vary:
Cookie` was already present on every response before this hook existed --
Flask's own `save_session()` adds it whenever `session.accessed`, and
`ensure_session_ids()` reads the session on every request regardless of
route. The hook's Vary line converts that incidental behavior into a
declared one rather than closing a real gap; `Cache-Control: private` is
the half of this hook with no pre-existing equivalent. See the per-test
comments below and FIX.md for the sabotage evidence proving this.

Cases are DERIVED from `jobcannon.web.PUBLIC_PATHS` itself, not a hand-typed
list — a future PUBLIC_PATHS addition is automatically covered here with no
second place to remember to update. All six routes need real Postgres:
/demo, /start, and /preview each read the DB directly (jobcannon/web/pages.py,
onboarding.py); /privacy, /terms, and /healthz don't strictly need it, but
sharing one fixture across all six keeps the parametrization genuinely
generic instead of special-casing DB-free routes.

Every case asserts `resp.status_code == 200` FIRST, before any header
assertion: `after_request` fires on error responses too, so a route that
silently 500s or redirects would still carry the hook's headers and this
test would go green over a broken route without the status check.
"""

from __future__ import annotations

import time

import pytest

from jobcannon.web import PUBLIC_PATHS
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

_DB_UNREACHABLE_RETRIES = 3
_DB_UNREACHABLE_BACKOFF_S = 0.3


def _get_tolerating_transient_db_unreachable(client, path):
    """GET `path`, absorbing a transient `/healthz`-shaped 503 (issue #250:
    flaked once on a self-hosted runner under concurrent load — the app
    could not reach Postgres for an instant). This test asserts cache
    headers, not DB liveness, so a live-probe hiccup is incidental to what
    it verifies.

    Scoped narrowly on purpose: only a response that matches the two
    discriminating fields of healthz's declared `{"status": "unhealthy",
    "db": "unreachable"}, 503` contract (503 + `db == "unreachable"`; see
    jobcannon/web/__init__.py's `healthz()`) gets retried. Any other status,
    or a 503 with a different body, returns immediately — a genuine
    persistent outage still fails loudly, as does a real bug in another
    route. /demo, /start, and /preview also touch Postgres directly but have
    no such controlled-503 contract of their own, so this helper is applied
    uniformly to the one shared `client.get(path)` call site below rather
    than special-cased to `/healthz`, and simply never matches for the
    other paths."""
    resp = client.get(path)
    for _ in range(_DB_UNREACHABLE_RETRIES):
        if resp.status_code != 503 or not resp.is_json:
            break
        body = resp.get_json(silent=True) or {}
        if body.get("db") != "unreachable":
            break
        time.sleep(_DB_UNREACHABLE_BACKOFF_S)
        resp = client.get(path)
    return resp


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_public_cache_headers")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "VERIFY_REQUEST": lambda r: None,
                "WEBHOOK_SECRET": "whsec_dGVzdA==",
            }
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def _vary_values(resp) -> set[str]:
    raw = resp.headers.get("Vary") or ""
    return {v.strip() for v in raw.split(",") if v.strip()}


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
def test_public_path_carries_vary_cookie_and_private_cache_control(path, app):
    client = app.test_client()

    resp = _get_tolerating_transient_db_unreachable(client, path)

    assert resp.status_code == 200, (path, resp.status_code, resp.data[:200])

    # Cache-Control is checked FIRST and is the discriminating half of this
    # test: it has no source other than the hook (or legal.py's own
    # _legal_response for /privacy, /terms), so it fails when the hook is
    # removed. Sabotage-verified: with @app.after_request commented out,
    # /demo, /healthz, /preview, /start all failed exactly this assertion.
    cache_control = resp.headers.get("Cache-Control") or ""
    assert "private" in cache_control, (path, cache_control)

    # Vary is checked SECOND and does NOT isolate the hook's contribution:
    # Flask's own SecureCookieSessionInterface.save_session() already adds
    # `Vary: Cookie` whenever `session.accessed`, and ensure_session_ids()
    # (jobcannon/web/anon_session.py:44) reads the session on every request
    # in this app, so this line stays green even with the hook fully
    # disabled (sabotage-verified: all 6 routes still passed this specific
    # assertion). It is asserted anyway because it pins the real invariant
    # the hook exists to guarantee explicitly rather than incidentally --
    # not because this test proves the hook is what produces it.
    assert "Cookie" in _vary_values(resp), (path, resp.headers.get("Vary"))


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_response_max_age_survives_the_shared_hook_unchanged(path, app):
    """The hook's guard (`if "Cache-Control" not in response.headers`) must
    never fire for /privacy or /terms — jobcannon/web/legal.py's
    `_legal_response` already sets one. Pins the exact string so a bug that
    made the hook overwrite rather than skip would be caught here, not just
    by the pre-existing tests/host/test_legal_pages.py assertions this
    duplicates on purpose (belt-and-suspenders: this file is what a reviewer
    checks first when auditing the new hook itself)."""
    client = app.test_client()

    resp = client.get(path)

    assert resp.headers["Cache-Control"] == "private, max-age=300"
    assert "Cookie" in _vary_values(resp)
