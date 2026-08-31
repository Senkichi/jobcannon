"""GET /postings/<id>/detail (jobcannon/web/posting_detail.py) — the
expandable card's stateless public fragment (spec §3). Route tests use the
same local-_app + monkeypatched-module-attribute pattern as
tests/host/test_pages.py; no Postgres needed except the final round-trip
test, which carries the requires_postgres marker."""

import contextlib
import datetime
import logging

from jobcannon.db._posting_detail import get_posting_detail
from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.posting_detail as posting_detail_module
from tests.host.conftest import requires_postgres

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def _app(verify=lambda req: None):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def _patch(monkeypatch, row):
    monkeypatch.setattr(
        posting_detail_module,
        "connection_factory",
        lambda: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(posting_detail_module, "get_posting_detail", lambda conn, posting_id: row)


def _row(**overrides):
    row = {
        "id": 7,
        "title": "Staff Engineer",
        "company": "Acme",
        "location": "Remote",
        "workplace_type": "REMOTE",
        "jd_full": "First paragraph.\n\nSecond paragraph.",
        "description": "Short description.",
        "comp_data_json": None,
        "locations_structured": None,
        "sightings": [],
        "source_urls": [],
        "posted_date": None,
        "posted_date_precision": None,
        "last_seen": None,
        "structural_axes": None,
        "structural_scored_at": None,
    }
    row.update(overrides)
    return row


def test_anonymous_get_renders_jd_full(monkeypatch):
    _patch(monkeypatch, _row())
    resp = _app().test_client().get("/postings/7/detail")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "data-posting-detail-panel" in html
    assert "First paragraph." in html
    assert "Second paragraph." in html
    assert "data-action-collapse" in html


def test_authed_get_renders_identically(monkeypatch):
    _patch(monkeypatch, _row())
    identity = ClerkIdentity(user_id="user_123", claims={"sub": "user_123"})
    resp = _app(verify=lambda req: identity).test_client().get("/postings/7/detail")
    assert resp.status_code == 200
    assert "data-posting-detail-panel" in resp.get_data(as_text=True)


def test_description_fallback_then_honest_note(monkeypatch):
    _patch(monkeypatch, _row(jd_full=None))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "Short description." in html

    _patch(monkeypatch, _row(jd_full=None, description=None))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "Full description not yet available for this posting." in html


def test_unknown_id_is_404(monkeypatch):
    _patch(monkeypatch, None)
    assert _app().test_client().get("/postings/999/detail").status_code == 404


def test_db_outage_renders_unavailable_fragment_not_404_or_500(monkeypatch, caplog):
    """#261: a DB outage must never surface as abort(404) (that claims the
    posting doesn't exist, which is false during an outage) nor as an
    unhandled 500 -- both leave the expand slot either wrongly labeled or,
    for a 5xx, entirely unswapped (htmx 2.0.4's default responseHandling
    maps 4xx/5xx to `{swap: false}`), which is the dead-click this issue
    exists to prevent. Monkeypatches connection_factory itself (not
    get_posting_detail) so the raise happens exactly where a real pool
    outage would surface it -- inside the `with connection_factory() as
    conn:` block, before get_posting_detail is ever reached."""

    def _boom():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(posting_detail_module, "connection_factory", _boom)

    with caplog.at_level(logging.WARNING):
        resp = _app().test_client().get("/postings/7/detail")
    html = resp.get_data(as_text=True)

    # Still 200: the swap target actually receives content (no dead click).
    assert resp.status_code == 200
    assert "data-detail-unavailable" in html
    assert "data-posting-detail-panel" not in html
    assert any("posting detail" in rec.message.lower() for rec in caplog.records)


def test_post_is_405_not_401(monkeypatch):
    # public_get opens GET/HEAD/OPTIONS only; an unregistered method on the
    # matched rule must surface as 405 via the routing_exception re-raise.
    _patch(monkeypatch, _row())
    assert _app().test_client().post("/postings/7/detail").status_code == 405


def test_null_axes_render_pending_marker_and_null_fields_say_not_specified(monkeypatch):
    _patch(monkeypatch, _row(location=None))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "signals still computing for this posting" in html
    assert "Not specified" in html
    assert "No confirmed post date" in html


def test_axes_render_dynamically_with_scored_at(monkeypatch):
    _patch(
        monkeypatch,
        _row(
            structural_axes={
                "freshness": {"value": 0.7},
                "seniority_clarity": {"value": True},
            },
            structural_scored_at=datetime.datetime(
                2026, 8, 15, 12, 30, tzinfo=datetime.timezone.utc
            ),
        ),
    )
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "freshness" in html
    assert "seniority clarity" in html
    assert "2026-08-15 12:30" in html
    assert "signals still computing" not in html


def test_comp_context_via_plain_dict(monkeypatch):
    # build_comp_context reads via .get(); the route must hand it a plain
    # dict, never the HybridRow. Patch it at the route module to observe
    # the payload shape.
    seen = {}

    def _fake_comp(job_row):
        seen.update(job_row)
        return "comp context line"

    _patch(monkeypatch, _row(comp_data_json='{"anything": true}'))
    monkeypatch.setattr(posting_detail_module, "build_comp_context", _fake_comp)
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "comp context line" in html
    assert seen == {"comp_data_json": '{"anything": true}'}


def test_timeline_and_sightings(monkeypatch):
    _patch(
        monkeypatch,
        _row(
            posted_date=datetime.date(2026, 8, 1),
            posted_date_precision="approximate",
            last_seen=datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.timezone.utc),
            sightings=[
                {
                    "source": "lever",
                    "source_url": "https://jobs.lever.co/acme/7",
                    "first_seen": "2026-08-01T00:00:00+00:00",
                    "last_seen": "2026-08-20T00:00:00+00:00",
                }
            ],
        ),
    )
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "Posted 2026-08-01" in html
    assert "(approximate)" in html
    assert "Last seen 2026-08-20 09:00" in html
    assert "lever" in html
    assert "first 2026-08-01" in html


def test_proxy_precision_never_claims_a_post_date(monkeypatch):
    # Same anchor-trust rule as jobcannon/web/why.py's freshness chips:
    # 'proxy' precision must not render as an origination date.
    _patch(monkeypatch, _row(posted_date=datetime.date(2026, 8, 1), posted_date_precision="proxy"))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "No confirmed post date" in html
    assert "Posted 2026-08-01" not in html


@requires_postgres
def test_get_posting_detail_round_trip(db_conn):
    # Same throwaway-DB seeding shape as tests/host/test_feed_page.py's
    # _seed_company / _seed_posting, but through the rollback-isolated
    # db_conn fixture (conftest) so the insert and the read share one
    # transaction — no separate autocommit connection needed here.
    company_id = db_conn.execute(
        "INSERT INTO companies (name) VALUES (%s) RETURNING id",
        ("Detail Test Co",),
    ).fetchone()["id"]
    seeded_id = db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("detail-test-1", company_id, "Detail Test Title", "Detail Test Co"),
    ).fetchone()["id"]

    row = get_posting_detail(db_conn, seeded_id)
    assert row is not None
    assert row["title"] is not None
    assert get_posting_detail(db_conn, -1) is None
