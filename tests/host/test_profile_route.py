"""GET/POST /profile (jobcannon/web/profile.py) — Spec 2's editor-first
profile page. Monkeypatched-module-attribute pattern (tests/host/test_pages.py
style): the DAL functions the route module imported are replaced on the
module, so no Postgres is needed; the DB-backed CSRF cases live in
tests/host/test_csrf.py."""

from __future__ import annotations

import contextlib
from decimal import Decimal

from flask import url_for
import pytest

from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.profile as profile_module

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="
USER_ID = "user_profile_123"


def _app(verify=lambda req: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID})):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def _row(**overrides):
    row = {
        "user_id": USER_ID,
        "skills": ["python", "retired-skill"],
        "experience_summary": "Twelve years.\nMostly backend.",
        "target_titles": ["Staff Engineer", "Principal Engineer"],
        "target_locations": ["Seattle, WA"],
        "seniority_level": "staff",
        "years_of_experience": Decimal("12.5"),
        "comp_floor_usd": 180000,
        "target_companies": ["Acme"],
        "workplace_type": "REMOTE",
        "updated_at": None,
    }
    row.update(overrides)
    return row


@pytest.fixture()
def db(monkeypatch):
    """Stub every DAL call the route module makes. Returns a dict the test
    can inspect: `writes` collects replace_profile kwargs."""
    state = {"row": _row(), "saved": 2, "pipeline": {"applied": 1, "dismissed": 3}, "writes": []}
    monkeypatch.setattr(
        profile_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(profile_module, "get_profile", lambda conn, user_id: state["row"])
    monkeypatch.setattr(
        profile_module, "count_saved_postings", lambda conn, user_id: state["saved"]
    )
    monkeypatch.setattr(
        profile_module, "count_pipeline_statuses", lambda conn, user_id: dict(state["pipeline"])
    )
    monkeypatch.setattr(
        profile_module,
        "replace_profile",
        lambda conn, user_id, **kw: state["writes"].append((user_id, kw)),
    )
    return state


def _valid_body(**overrides):
    body = {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme",
        "target_locations": "Seattle, WA\nRemote",
        "experience_summary": "Twelve years.",
        "skills": ["python", "sql"],
        "seniority_level": "staff",
        "years_of_experience": "12.5",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }
    body.update(overrides)
    return body


# --- routing / auth -------------------------------------------------------


def test_unauthenticated_get_and_post_are_401(db):
    client = _app(verify=lambda req: None).test_client()

    assert client.get("/profile").status_code == 401
    assert client.post("/profile", data=_valid_body()).status_code == 401
    assert db["writes"] == []


def test_url_for_profile_edit_is_exactly_slash_profile():
    """Tasks 5 and 6 redirect/link to the literal "/profile" (they land in
    Wave 1, before this blueprint exists); this is the pin that keeps the
    literal honest."""
    app = _app()
    with app.test_request_context("/"):
        assert url_for("profile.edit") == "/profile"
        assert url_for("profile.submit") == "/profile"


# --- GET ----------------------------------------------------------------


def test_get_prefills_every_field_from_the_row(db):
    html = _app().test_client().get("/profile").get_data(as_text=True)

    assert "Staff Engineer\nPrincipal Engineer</textarea>" in html
    assert ">Acme</textarea>" in html
    assert ">Seattle, WA</textarea>" in html
    assert "Twelve years.\nMostly backend.</textarea>" in html
    assert 'value="python" checked' in html
    assert 'value="sql"' in html and 'value="sql" checked' not in html
    assert "retired-skill" not in html  # filtered by SKILLS_OPTIONS
    assert '<option value="staff" selected>' in html
    assert 'name="years_of_experience"' in html and 'value="12.5"' in html
    assert 'name="comp_floor_usd"' in html and 'value="180000"' in html
    assert '<option value="remote" selected>' in html
    assert 'name="csrf_token"' in html
    assert 'action="/profile"' in html
    assert 'method="post"' in html


def test_get_with_no_row_renders_a_blank_form(db):
    """Spec §2 no-row edge case: a user who signed up without onboarding has
    no profiles row; the form renders empty and the first POST creates it."""
    db["row"] = None
    resp = _app().test_client().get("/profile")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'action="/profile"' in html
    assert "checked" not in html
    assert '<option value="" selected>' in html  # blank seniority + workplace
    assert "data-profile-unavailable" not in html


def test_get_renders_the_three_stat_links_in_history_order(db):
    html = _app().test_client().get("/profile").get_data(as_text=True)

    saved = html.index('data-profile-stat="saved"')
    applied = html.index('data-profile-stat="applied"')
    dismissed = html.index('data-profile-stat="dismissed"')
    assert saved < applied < dismissed
    assert 'href="/postings?view=saved"' in html
    assert 'href="/postings?view=applied"' in html
    assert 'href="/postings?view=dismissed"' in html
    assert ">2</span>" in html  # saved
    assert ">1</span>" in html  # applied
    assert ">3</span>" in html  # dismissed


def test_get_renders_zero_counts_rather_than_hiding_cells(db):
    db["saved"] = 0
    db["pipeline"] = {"applied": 0, "dismissed": 0}
    html = _app().test_client().get("/profile").get_data(as_text=True)

    assert html.count(">0</span>") == 3
    assert html.count("data-profile-stat=") == 3


def test_get_saved_flag_renders_the_confirmation(db):
    html = _app().test_client().get("/profile?saved=1").get_data(as_text=True)
    assert "data-profile-saved" in html
    assert "Profile saved." in html

    html = _app().test_client().get("/profile").get_data(as_text=True)
    assert "data-profile-saved" not in html


def test_get_fails_closed_when_the_read_fails(db, monkeypatch):
    """A blank form on a failed read would invite the visitor to save an
    empty snapshot over a profile that exists — destructive. The read
    failure renders an unavailable notice and NO form, at 200 (the page
    itself is fine; the data is not)."""

    def _boom(conn, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(profile_module, "get_profile", _boom)
    resp = _app().test_client().get("/profile")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "data-profile-unavailable" in html
    assert 'action="/profile"' not in html
    assert "data-profile-stat=" not in html


# --- POST ---------------------------------------------------------------


def test_post_valid_snapshot_writes_and_redirects(db):
    resp = _app().test_client().post("/profile", data=_valid_body())

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile?saved=1")
    assert db["writes"] == [
        (
            USER_ID,
            {
                "skills": ["python", "sql"],
                "experience_summary": "Twelve years.",
                "target_titles": ["Staff Engineer", "Principal Engineer"],
                "target_locations": ["Seattle, WA", "Remote"],
                "seniority_level": "staff",
                "years_of_experience": 12.5,
                "comp_floor_usd": 180000,
                "target_companies": ["Acme"],
                "workplace_type": "REMOTE",
            },
        )
    ]


def test_post_blank_form_clears_everything(db):
    """Empty list = deliberate clear; blank scalar = NULL. The whole point of
    replace_profile over upsert_profile (plan Deviation 1)."""
    resp = _app().test_client().post("/profile", data={"workplace_type": ""})

    assert resp.status_code == 303
    _, kw = db["writes"][0]
    assert kw == {
        "skills": [],
        "experience_summary": None,
        "target_titles": [],
        "target_locations": [],
        "seniority_level": None,
        "years_of_experience": None,
        "comp_floor_usd": None,
        "target_companies": [],
        "workplace_type": None,
    }


def test_post_validation_error_rerenders_200_echoing_every_field(db):
    body = _valid_body(years_of_experience="lots", target_locations="Paris\nBerlin")
    resp = _app().test_client().post("/profile", data=body)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Location" not in resp.headers
    assert db["writes"] == []
    assert "years of experience must be a number" in html
    assert 'value="lots"' in html  # the bad value echoes, not the stored one
    assert "Paris\nBerlin</textarea>" in html
    assert "Staff Engineer\nPrincipal Engineer</textarea>" in html
    assert 'value="python" checked' in html
    assert 'value="sql" checked' in html
    assert '<option value="remote" selected>' in html
    assert "data-profile-stat=" in html  # stats strip still present on the error page


def test_post_write_failure_is_a_500_not_a_silent_success(db, monkeypatch):
    def _boom(conn, user_id, **kw):
        raise RuntimeError("write failed")

    monkeypatch.setattr(profile_module, "replace_profile", _boom)
    app = _app()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = app.test_client().post("/profile", data=_valid_body())

    assert resp.status_code == 500
