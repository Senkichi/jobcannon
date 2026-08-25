"""jobcannon/web/privacy.py — GET /privacy: the static, public placeholder
scaffold for issue #94.

No DB access, so tests here need no throwaway Postgres (unlike
tests/host/test_consent_route.py's record_consent-backed tests) — same
no-DB shape as tests/host/test_pages.py's /demo tests. The template-link
assertions read the .html sources directly rather than rendering through
Flask, the same relative-path-from-repo-root pattern
test_consent_route.py's test_no_python_wallclock_in_the_consent_route
uses for its .py source reads.
"""

from __future__ import annotations

import pathlib


def _app(verify=None):
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        }
    )


def test_privacy_is_in_public_paths():
    from jobcannon.web import PUBLIC_PATHS

    assert "/privacy" in PUBLIC_PATHS


def test_privacy_page_renders_unauthed_200():
    """The footer link is unconditional (renders on error_401.html too),
    so an unauthed visitor clicking it must never hit the 401 wall."""
    app = _app(verify=lambda req: None)
    client = app.test_client()

    resp = client.get("/privacy")

    assert resp.status_code == 200
    assert b"Placeholder" in resp.data


def test_privacy_page_renders_authed_200():
    from jobcannon.web.auth import ClerkIdentity

    app = _app(verify=lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}))
    client = app.test_client()

    resp = client.get("/privacy")

    assert resp.status_code == 200


def test_footer_links_to_privacy():
    src = pathlib.Path("jobcannon/web/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/privacy"' in src


def test_consent_copy_links_to_privacy():
    src = pathlib.Path("jobcannon/web/templates/consent.html").read_text(encoding="utf-8")
    assert 'href="/privacy"' in src


def test_privacy_template_is_a_clearly_marked_placeholder():
    src = pathlib.Path("jobcannon/web/templates/privacy.html").read_text(encoding="utf-8")
    assert "Placeholder" in src
    assert 'extends "base.html"' in src
