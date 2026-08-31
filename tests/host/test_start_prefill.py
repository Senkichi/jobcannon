"""/preview's switch to build_entry (Spec 1 Task 5). Route test with a
monkeypatched module attribute, same pattern as tests/host/test_pages.py —
no Postgres needed. (This file also held the GET /start profile-prefill
tests until Spec 2 removed the prefill: a signed-in visitor is now 303'd to
/profile before the picker renders — see tests/host/test_start_authed_redirect.py.)"""

from jobcannon.web import create_app
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
