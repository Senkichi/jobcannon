"""Picker-first onboarding: GET/POST /start (jobcannon/web/onboarding.py).

Own throwaway database, same shape as tests/host/test_webhooks.py: POST
/start does real, durable INSERT/UPSERT writes on users/profiles, so this
module cannot share the session-scoped postgres_test_dsn every other
tests/host/ module reads inside a rollback-isolated transaction.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


SEEDED_COMPANY = "Onboarding Test Co"


def _seed_postings(dsn: str, titles: list[str]) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (SEEDED_COMPANY,)
        ).fetchone()[0]
        for i, title in enumerate(titles):
            conn.execute(
                "INSERT INTO postings (dedup_key, company_id, title, company) "
                "VALUES (%s, %s, %s, %s)",
                (f"onboarding-{i}", company_id, title, SEEDED_COMPANY),
            )


def _anon_user_count(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT count(*) FROM users WHERE id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()[0]


def _profile_row_for_anon(dsn: str):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT p.* FROM profiles p JOIN users u ON u.id = p.user_id WHERE u.id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_onboarding")
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


def test_start_is_reachable_unauthenticated(app):
    """Today every non-PUBLIC_PATHS path 401s (jobcannon/web/__init__.py) —
    /start must be exempt and render, with no Clerk credentials at all."""
    resp = app.test_client().get("/start")
    assert resp.status_code == 200
    assert "Tell us what you're looking for" in resp.get_data(as_text=True)


def test_picker_options_come_from_the_corpus_not_a_literal_list(app):
    """Positive control: a distinctively-named seeded posting title AND its
    company must both appear in the rendered picker, proving both option
    lists round-trip through the corpus query rather than a hardcoded list
    (which would never contain either string)."""
    _seed_postings(app.config["_TEST_DSN"], ["Distinctive Corpus Title Alpha"])

    html = app.test_client().get("/start").get_data(as_text=True)

    assert "Distinctive Corpus Title Alpha" in html
    assert SEEDED_COMPANY in html


def test_picker_submit_writes_profile_row_through_upsert_profile(app):
    """A stranger completes the picker without supplying any personal data:
    after one anonymous POST /start, a profiles row exists, and no
    email/name/free-text field was supplied or stored."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "titles": ["Engineer"],
            "skills": ["python", "sql"],
            "seniority_level": "senior",
            "years_of_experience": "5",
            "workplace_type": "remote",
        },
    )
    assert resp.status_code in (302, 303)

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row is not None
    assert row["seniority_level"] == "senior"
    assert sorted(row["skills"]) == ["python", "sql"]

    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        email = conn.execute(
            "SELECT email FROM users WHERE id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()[0]
    assert email is None

    # The session must carry a token postings.workplace_type can actually
    # equal (uppercase, per jobcannon/engine/location_canonical.py's
    # WorkplaceType), not the form's lowercase "remote" verbatim — a later
    # PR wires this session value straight into a `= %s` filter with no
    # case-folding on either side.
    with client.session_transaction() as sess:
        assert sess["pending_picker"]["workplace_type"] == "REMOTE"


def test_picker_submit_mints_exactly_one_anon_user_row(app):
    client = app.test_client()
    client.post(
        "/start",
        data={"seniority_level": "senior", "years_of_experience": "5", "workplace_type": "any"},
    )
    assert _anon_user_count(app.config["_TEST_DSN"]) == 1


def test_repeat_submit_reuses_the_same_anon_id(app):
    client = app.test_client()
    client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})
    client.post("/start", data={"seniority_level": "staff", "workplace_type": "any"})

    assert _anon_user_count(app.config["_TEST_DSN"]) == 1
    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["seniority_level"] == "staff"


def test_target_locations_and_experience_summary_are_not_written(app):
    client = app.test_client()
    client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["target_locations"] is None
    assert row["experience_summary"] is None


def test_invalid_seniority_rerenders_form_without_writing(app):
    client = app.test_client()
    resp = client.post("/start", data={"seniority_level": "bogus", "workplace_type": "any"})

    assert resp.status_code == 200
    assert "unrecognized seniority level" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0


def test_failed_submit_leaves_no_orphan_anon_user_row(app, monkeypatch):
    """mint_anon_user and upsert_profile must share one transaction, not one
    connection with two independent commits: if the second write raises, the
    first must roll back too, or a stranger's retry mints a second anon row
    forever (falsifying test_picker_submit_mints_exactly_one_anon_user_row on
    the failure path)."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure between mint and upsert")

    monkeypatch.setattr("jobcannon.web.onboarding.upsert_profile", _boom)

    client = app.test_client()
    with pytest.raises(RuntimeError):
        client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})

    assert _anon_user_count(app.config["_TEST_DSN"]) == 0


def test_repeat_get_start_after_submit_shows_completion_state(app):
    """The redirect target, confirmed working on its own: GET /start after
    a completed POST /start renders the "preview coming next" confirmation,
    not a 401 or a 500."""
    client = app.test_client()
    post_resp = client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})
    assert post_resp.status_code in (302, 303)

    resp = client.get("/start")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "preview coming next" in html.lower()
