"""/start is a purely anonymous surface (Spec 2 decision 2, issue #262): a
visitor whose Clerk identity resolves is 303'd to /profile on GET and POST
alike, before any other branch runs. Monkeypatched-module-attribute pattern
(tests/host/test_pages.py style), no Postgres needed."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.onboarding as onboarding_module

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def _app(verify=lambda req: None):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def _identity():
    return ClerkIdentity(user_id="user_123", claims={"sub": "user_123"})


def _authed_app():
    return _app(verify=lambda req: _identity())


def _forbid_anon_writes(monkeypatch):
    """The anon/pending domain must gain no writes from an authed request:
    a call into either writer is the failure."""

    def _boom(*args, **kwargs):
        raise AssertionError("anon-domain writer called for an authed visitor")

    monkeypatch.setattr(onboarding_module, "mint_anon_user", _boom)
    monkeypatch.setattr(onboarding_module, "upsert_profile", _boom)
    monkeypatch.setattr(onboarding_module, "connection_factory", _boom)


def test_authed_get_start_redirects_to_profile():
    resp = _authed_app().test_client().get("/start")

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_get_start_with_query_still_redirects():
    """The search form's own GET fallback (?q=) is a /start GET too."""
    resp = _authed_app().test_client().get("/start?q=staff&titles=Engineer")

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_hx_get_start_redirects_before_the_fragment_branch():
    """The identity check runs FIRST: an HX-Request from a signed-in visitor
    gets the same 303, never a #picker-options fragment. Unreachable from the
    picker in practice (an authed visitor never renders it), pinned so the
    invariant is 'authed never touches /start', not 'usually'."""
    resp = _authed_app().test_client().get("/start?q=x", headers={"HX-Request": "true"})

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_post_start_redirects_and_writes_nothing(monkeypatch):
    _forbid_anon_writes(monkeypatch)

    resp = (
        _authed_app()
        .test_client()
        .post("/start", data={"titles": ["Engineer"], "seniority_level": ""})
    )

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_redirect_carries_the_public_path_cache_headers():
    """Spec §1 cache safety: /start is PUBLIC_PATHS and now returns an
    identity-dependent response, so the shared-cache guard the after_request
    hook applies to every PUBLIC_PATHS response must be on the 303 too."""
    resp = _authed_app().test_client().get("/start")

    assert resp.status_code == 303
    assert "Cookie" in resp.headers.get("Vary", "")
    assert "private" in resp.headers.get("Cache-Control", "")


def test_anonymous_get_start_renders_the_picker(monkeypatch):
    """Byte-identical anonymous flow: no redirect, the picker form renders."""
    monkeypatch.setattr(
        onboarding_module, "_read_picker_options", lambda q="": {"titles": [], "companies": []}
    )
    resp = _app().test_client().get("/start")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'action="/start"' in html
    assert "Location" not in resp.headers


def test_anonymous_invalid_post_start_still_rerenders_200():
    resp = _app().test_client().post("/start", data={"seniority_level": ""})

    assert resp.status_code == 200
    assert "pick at least one title or company" in resp.get_data(as_text=True)


def test_anonymous_valid_post_start_still_redirects_to_preview(monkeypatch):
    """The anon happy path is unchanged: mint, upsert, 302 to /preview.
    start_submit opens `conn.raw.transaction()` around the two writes, so
    the connection double needs a `.raw` with a no-op transaction()."""
    calls = []
    conn = SimpleNamespace(raw=SimpleNamespace(transaction=lambda: contextlib.nullcontext()))
    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(conn)
    )
    monkeypatch.setattr(
        onboarding_module, "mint_anon_user", lambda conn: calls.append("mint") or "anon_abc"
    )
    monkeypatch.setattr(
        onboarding_module, "upsert_profile", lambda conn, user_id, **kw: calls.append("upsert")
    )

    resp = _app().test_client().post("/start", data={"titles": ["Engineer"], "seniority_level": ""})

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/preview")
    assert calls == ["mint", "upsert"]


def test_verifier_failure_is_treated_as_anonymous(monkeypatch):
    """_current_identity fails OPEN: a throwing verifier means 'anonymous',
    i.e. today's exact behavior — a form, not a 500 and not a redirect."""
    monkeypatch.setattr(
        onboarding_module, "_read_picker_options", lambda q="": {"titles": [], "companies": []}
    )

    def _boom(req):
        raise RuntimeError("clerk unreachable")

    resp = _app(verify=_boom).test_client().get("/start")

    assert resp.status_code == 200
    assert "Location" not in resp.headers
