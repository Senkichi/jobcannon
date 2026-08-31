"""GET /start profile prefill (spec §5) + /preview's switch to build_entry.
Route/unit tests with monkeypatched module attributes, same pattern as
tests/host/test_pages.py — no Postgres needed."""

import contextlib
from decimal import Decimal

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


def _profile_row(**overrides):
    row = {
        "user_id": "user_123",
        "target_titles": ["Staff Engineer"],
        "target_companies": ["Acme"],
        "skills": ["python", "not-a-known-skill"],
        "seniority_level": "staff",
        "years_of_experience": Decimal("12"),
        "comp_floor_usd": 180000,
        "workplace_type": "REMOTE",
    }
    row.update(overrides)
    return row


def _patch_db(monkeypatch, row):
    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(onboarding_module, "get_profile", lambda conn, user_id: row)


def test_profile_prefill_maps_row_to_form_values(monkeypatch):
    _patch_db(monkeypatch, _profile_row())
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {
            "checked_titles": ["Staff Engineer"],
            "checked_companies": ["Acme"],
            "checked_skills": ["python"],  # unknown skill filtered out
            "seniority_level": "staff",
            "years_of_experience": "12",
            "comp_floor_usd": "180000",
            "workplace_type": "remote",  # DB 'REMOTE' -> form value
        }


def test_profile_prefill_anonymous_is_empty(monkeypatch):
    _patch_db(monkeypatch, _profile_row())
    with _app().test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {}


def test_profile_prefill_no_row_is_empty(monkeypatch):
    _patch_db(monkeypatch, None)
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {}


def test_profile_prefill_fails_open_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(onboarding_module, "connection_factory", _boom)
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {}


def test_profile_prefill_null_fields_echo_as_blank(monkeypatch):
    _patch_db(
        monkeypatch,
        _profile_row(
            target_titles=None,
            target_companies=None,
            skills=None,
            seniority_level=None,
            years_of_experience=None,
            comp_floor_usd=None,
            workplace_type=None,
        ),
    )
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {
            "checked_titles": [],
            "checked_companies": [],
            "checked_skills": [],
            "seniority_level": "",
            "years_of_experience": "",
            "comp_floor_usd": "",
            "workplace_type": "",
        }


def test_start_get_prefills_from_profile(monkeypatch):
    _patch_db(monkeypatch, _profile_row())
    monkeypatch.setattr(
        onboarding_module,
        "_read_picker_options",
        lambda q="": {"titles": ["Backend Engineer"], "companies": ["Other Co"]},
    )
    app = _app(verify=lambda req: _identity())
    html = app.test_client().get("/start").get_data(as_text=True)
    # _merge_checked folds the saved title/company into the rendered
    # options even though the corpus window doesn't list them.
    assert "Staff Engineer" in html
    assert "Acme" in html


def test_start_get_carry_forward_beats_prefill(monkeypatch):
    calls = []

    def _get_profile(conn, user_id):
        calls.append(user_id)
        return _profile_row()

    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(onboarding_module, "get_profile", _get_profile)
    monkeypatch.setattr(
        onboarding_module,
        "_read_picker_options",
        lambda q="": {"titles": ["Backend Engineer"], "companies": []},
    )
    app = _app(verify=lambda req: _identity())
    html = app.test_client().get("/start?titles=Backend+Engineer").get_data(as_text=True)
    assert calls == []  # explicit carry-forward: the DB is never read
    assert "Backend Engineer" in html


def test_start_hx_fragment_never_prefills(monkeypatch):
    calls = []

    def _get_profile(conn, user_id):
        calls.append(user_id)
        return _profile_row()

    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(onboarding_module, "get_profile", _get_profile)
    monkeypatch.setattr(
        onboarding_module,
        "_read_picker_options",
        lambda q="": {"titles": [], "companies": []},
    )
    app = _app(verify=lambda req: _identity())
    resp = app.test_client().get("/start?q=eng", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert calls == []  # a fragment render must never re-check unchecked boxes


def test_preview_entries_come_from_build_entry(monkeypatch):
    row = {
        "id": 1,
        "title": "Staff Engineer",
        "company": "Acme",
        "location": "Remote",
        "workplace_type": "REMOTE",
        "salary_min": 150000,
        "salary_max": 200000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "structural_axes": None,
        "posted_date": None,
        "posted_date_precision": None,
        "last_seen": None,
        "rank_score": None,
        "saved": None,
        "applied": None,
        "source_urls": ["https://jobs.example/1"],
        "sightings": [],
    }
    monkeypatch.setattr(onboarding_module, "_read_preview_postings", lambda **kwargs: [row])
    resp = _app().test_client().get("/preview")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Staff Engineer" in html
