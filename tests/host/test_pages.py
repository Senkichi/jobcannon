"""GET / (authed feed shell) and GET /demo (public guest demo) — 1B Wave 3
PR 11's day-1-stranger prerequisites. No Postgres needed: corpus_stats /
get_profile are monkeypatched AS IMPORTED IN jobcannon.web.pages (module-
level names there, precisely so they are patchable) rather than exercised
against a real connection. connection_factory is patched the same way for
the two tests that need `_read_page_data` to actually reach corpus_stats —
otherwise (TESTING never opens a pool, same as test_auth.py's identity-only
tests) the fail-closed default already coincides with the values those two
tests want, which would make the assertions pass for the wrong reason."""

from __future__ import annotations

import contextlib

import pytest


def _app(verify=None):
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        }
    )


@pytest.fixture
def app_client_authed():
    from jobcannon.web.auth import ClerkIdentity

    app = _app(verify=lambda req: ClerkIdentity(user_id="user_123", claims={"sub": "user_123"}))
    return app.test_client()


@pytest.fixture
def app_client_unauthed():
    app = _app(verify=lambda req: None)
    return app.test_client()


def _patch_connection_factory(monkeypatch):
    """Makes `_read_page_data`'s `with connection_factory() as conn:` succeed
    with a throwaway sentinel conn — irrelevant here since corpus_stats/
    get_profile are patched to ignore it, but the with-block must not raise
    for those patches to ever run."""
    from jobcannon.web import pages

    monkeypatch.setattr(pages, "connection_factory", lambda: contextlib.nullcontext(object()))


def test_root_requires_auth(app_client_unauthed):
    assert app_client_unauthed.get("/").status_code == 401


def test_root_renders_no_profile_empty_state(app_client_authed, monkeypatch):
    from jobcannon.web import pages

    _patch_connection_factory(monkeypatch)
    monkeypatch.setattr(
        pages,
        "corpus_stats",
        lambda conn: {"postings": 0, "companies": 0, "freshest_last_seen": None},
    )
    monkeypatch.setattr(pages, "get_profile", lambda conn, user_id: None)

    html = app_client_authed.get("/").get_data(as_text=True)
    assert "Your feed isn't wired up yet" in html


def test_demo_fail_closed_when_connection_open_fails(app_client_unauthed, monkeypatch):
    """Proves _read_page_data's except clause actually engages when
    connection_factory() itself raises (unopened pool / outage) — the
    companion to the fix above: this is the ONE path the previous
    (unpatched) test_root_renders_no_profile_empty_state was accidentally
    exercising and mistaking for coverage of the mocked-DAL path."""
    from jobcannon.web import pages

    def _raise():
        raise RuntimeError("pool not opened")

    monkeypatch.setattr(pages, "connection_factory", _raise)

    response = app_client_unauthed.get("/demo")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "The corpus is warming up" in html


def test_demo_fail_closed_when_corpus_stats_raises(app_client_unauthed, monkeypatch):
    """Proves the except clause also covers errors from the DB reads
    themselves (corpus_stats/get_profile), not just a failed connection
    open — connection_factory succeeds here via nullcontext."""
    from jobcannon.web import pages

    _patch_connection_factory(monkeypatch)

    def _raise(conn):
        raise RuntimeError("query failed")

    monkeypatch.setattr(pages, "corpus_stats", _raise)

    response = app_client_unauthed.get("/demo")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "The corpus is warming up" in html


def test_demo_is_public_and_renders_corpus_stats(app_client_unauthed, monkeypatch):
    from jobcannon.web import pages

    _patch_connection_factory(monkeypatch)
    monkeypatch.setattr(
        pages,
        "corpus_stats",
        lambda conn: {"postings": 3, "companies": 2, "freshest_last_seen": None},
    )
    monkeypatch.setattr(pages, "get_profile", lambda conn, user_id: {"seniority_level": "senior"})

    response = app_client_unauthed.get("/demo")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "The live corpus" in html
    assert "3" in html


def test_demo_renders_warming_up_when_corpus_empty(app_client_unauthed, monkeypatch):
    from jobcannon.web import pages

    _patch_connection_factory(monkeypatch)
    monkeypatch.setattr(
        pages,
        "corpus_stats",
        lambda conn: {"postings": 0, "companies": 0, "freshest_last_seen": None},
    )
    monkeypatch.setattr(pages, "get_profile", lambda conn, user_id: None)

    response = app_client_unauthed.get("/demo")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "The corpus is warming up" in html


def test_demo_trailing_slash_is_public_not_401(app_client_unauthed, monkeypatch):
    """Regression test: /demo is registered strict_slashes=False, so /demo/
    resolves to the same view as /demo, but the public-path gate in
    jobcannon/web/__init__.py used to compare request.path (== "/demo/")
    against the exact-string PUBLIC_PATHS set (which only has "/demo") —
    an unauthenticated GET /demo/ hit abort(401) instead of serving the
    public page. The gate now strips a trailing slash before the
    membership check."""
    from jobcannon.web import pages

    _patch_connection_factory(monkeypatch)
    monkeypatch.setattr(
        pages,
        "corpus_stats",
        lambda conn: {"postings": 3, "companies": 2, "freshest_last_seen": None},
    )
    monkeypatch.setattr(pages, "get_profile", lambda conn, user_id: {"seniority_level": "senior"})

    response = app_client_unauthed.get("/demo/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "The live corpus" in html
