"""Host-dialect tests for jobcannon.db._conversion_metrics (ledger L-0066).

Seed helpers copied from tests/host/test_user_action_counts.py (same table
shapes). Read-only module -- no writer tests needed here.
"""

from __future__ import annotations

from jobcannon.db._conversion_metrics import compute_conversion_by_band
from jobcannon.db._user_actions import dismiss_posting, mark_applied
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def _seed_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (%s, %s, 'jobvite', %s, 'hit') RETURNING id",
        (name, name, name.lower().replace(" ", "-")),
    ).fetchone()["id"]


def _seed_scored_posting(conn, dedup_key, company_id, classification, *, title="Engineer"):
    return conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, "
        "classification, scoring_model, scoring_provider) "
        "VALUES (%s, %s, %s, 'Acme', %s, 'gpt', 'openai') RETURNING id",
        (dedup_key, company_id, title, classification),
    ).fetchone()["id"]


def test_every_band_present_even_at_zero(db_conn):
    _seed_user(db_conn, "conv-empty")

    result = compute_conversion_by_band(db_conn, "conv-empty")

    assert set(result.keys()) == {"apply", "consider", "skip", "reject", "low_signal"}
    for band in result.values():
        assert band == {"scored": 0, "applied": 0, "application_rate": None}


def test_scored_counts_are_global_not_per_user(db_conn):
    """scored is a corpus-wide count (every scored posting, no user_id
    filter) -- only the applied half of the cross-tab is user-scoped, per
    this row's adjudicated seam."""
    _seed_user(db_conn, "conv-u1")
    company = _seed_company(db_conn, "conv-scored-co")
    _seed_scored_posting(db_conn, "conv-scored|1", company, "apply")
    _seed_scored_posting(db_conn, "conv-scored|2", company, "apply")
    _seed_scored_posting(db_conn, "conv-scored|3", company, "reject")

    result = compute_conversion_by_band(db_conn, "conv-u1")

    assert result["apply"]["scored"] == 2
    assert result["reject"]["scored"] == 1
    assert result["apply"]["applied"] == 0
    assert result["apply"]["application_rate"] == 0.0


def test_application_rate_scoped_to_this_user_only(db_conn):
    _seed_user(db_conn, "conv-u2")
    _seed_user(db_conn, "conv-u3")
    company = _seed_company(db_conn, "conv-apply-co")
    p1 = _seed_scored_posting(db_conn, "conv-apply|1", company, "apply")
    p2 = _seed_scored_posting(db_conn, "conv-apply|2", company, "apply")

    mark_applied(db_conn, "conv-u2", p1)
    # conv-u3 applies to the OTHER posting -- must not count toward conv-u2's rate
    mark_applied(db_conn, "conv-u3", p2)

    result = compute_conversion_by_band(db_conn, "conv-u2")

    assert result["apply"]["scored"] == 2
    assert result["apply"]["applied"] == 1
    assert result["apply"]["application_rate"] == 0.5


def test_dismissed_status_does_not_count_as_applied(db_conn):
    _seed_user(db_conn, "conv-u4")
    company = _seed_company(db_conn, "conv-dismiss-co")
    p1 = _seed_scored_posting(db_conn, "conv-dismiss|1", company, "consider")

    dismiss_posting(db_conn, "conv-u4", p1)

    result = compute_conversion_by_band(db_conn, "conv-u4")

    assert result["consider"]["scored"] == 1
    assert result["consider"]["applied"] == 0
    assert result["consider"]["application_rate"] == 0.0


def test_unscored_postings_are_excluded(db_conn):
    """A posting with no scoring_model/classification (unscored) must not
    appear in any band's scored count."""
    _seed_user(db_conn, "conv-u5")
    company = _seed_company(db_conn, "conv-unscored-co")
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES ('conv-unscored|1', %s, 'Engineer', 'Acme')",
        (company,),
    )

    result = compute_conversion_by_band(db_conn, "conv-u5")

    assert all(band["scored"] == 0 for band in result.values())


def test_no_converted_or_callback_rate_keys(db_conn):
    """L-0066 scope note: private also returned converted/callback_rate
    (pipeline_events max-stage-ever, no host equivalent) -- confirm this
    port's dict shape does not carry those keys at all."""
    _seed_user(db_conn, "conv-shape")

    result = compute_conversion_by_band(db_conn, "conv-shape")

    for band in result.values():
        assert set(band.keys()) == {"scored", "applied", "application_rate"}
