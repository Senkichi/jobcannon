"""jobcannon.host.ingestion.capture.record_run -- the sole writer of
email_parse_log_sender (L-0279).

Not a straight port test file (capture.py itself IS a port -- see its own
module docstring); this suite is new, covering the seam edits capture.py's
docstring calls out: the PII scrub chokepoint on `last_error`, the D19
zero-count-row-per-known-sender behavior, the (user_id, run_id, sender_label)
ON CONFLICT dedup, and the never-raises contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobcannon.engine.email_senders import SENDERS
from jobcannon.host.ingestion import capture

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

_ALL_LABELS = frozenset(spec.label for spec in SENDERS)


def _seed_user(conn, user_id, email=None):
    conn.execute(
        "INSERT INTO users (id, email) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (user_id, email),
    )


def _rows_for(conn, user_id, run_id):
    return conn.execute(
        "SELECT sender_label, emails_seen, jobs_parsed, error_count, last_error "
        "FROM email_parse_log_sender WHERE user_id = %s AND run_id = %s "
        "ORDER BY sender_label",
        (user_id, run_id),
    ).fetchall()


def test_record_run_writes_zero_count_row_for_every_known_sender_with_no_activity(db_conn):
    """D19: a sender that saw zero emails this run still gets a row, so
    "no alert emails ever arrived" is distinguishable from "parser silently
    yields nothing"."""
    _seed_user(db_conn, "cap-u1")

    capture.record_run(
        db_conn,
        "cap-u1",
        run_id="run-1",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[],
        parse_failures=[],
    )

    rows = _rows_for(db_conn, "cap-u1", "run-1")
    labels_written = {r["sender_label"] for r in rows}
    assert labels_written == _ALL_LABELS
    for r in rows:
        assert r["emails_seen"] == 0
        assert r["jobs_parsed"] == 0
        assert r["error_count"] == 0
        assert r["last_error"] is None


def test_record_run_aggregates_counts_per_label(db_conn):
    _seed_user(db_conn, "cap-u2")

    capture.record_run(
        db_conn,
        "cap-u2",
        run_id="run-2",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[
            {"label": "linkedin", "job_count": 3},
            {"label": "linkedin", "job_count": 2},
            {"label": "glassdoor", "job_count": 0},
        ],
        parse_failures=[
            {"label": "linkedin", "error": "boom"},
        ],
    )

    rows = {r["sender_label"]: r for r in _rows_for(db_conn, "cap-u2", "run-2")}
    assert rows["linkedin"]["emails_seen"] == 2
    assert rows["linkedin"]["jobs_parsed"] == 5
    assert rows["linkedin"]["error_count"] == 1
    assert rows["linkedin"]["last_error"] == "boom"
    assert rows["glassdoor"]["emails_seen"] == 1
    assert rows["glassdoor"]["jobs_parsed"] == 0
    assert rows["glassdoor"]["error_count"] == 0


def test_record_run_scrubs_last_error_before_persisting(db_conn):
    """PII chokepoint (§6): a parser exception message embedding this
    tenant's own email address must never reach the stored last_error
    verbatim."""
    _seed_user(db_conn, "cap-u3", email="cap-u3@example.org")

    capture.record_run(
        db_conn,
        "cap-u3",
        run_id="run-3",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[],
        parse_failures=[
            {"label": "indeed", "error": "failed parsing body sent to cap-u3@example.org"},
        ],
    )

    rows = {r["sender_label"]: r for r in _rows_for(db_conn, "cap-u3", "run-3")}
    assert "cap-u3@example.org" not in rows["indeed"]["last_error"]


def test_record_run_scrubs_any_embedded_email_even_without_tenant_identifier(db_conn):
    """scrub_text's _EMAIL_RE redacts ANY email pattern, not just the
    tenant's own -- a parser exception can embed a slice of the raw body,
    which may carry someone else's address (e.g. a job poster's contact)."""
    _seed_user(db_conn, "cap-u4")

    capture.record_run(
        db_conn,
        "cap-u4",
        run_id="run-4",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[],
        parse_failures=[
            {"label": "trueup", "error": "contact recruiter@other-company.com for details"},
        ],
    )

    rows = {r["sender_label"]: r for r in _rows_for(db_conn, "cap-u4", "run-4")}
    assert "recruiter@other-company.com" not in rows["trueup"]["last_error"]


def test_record_run_on_conflict_dedups_same_user_run_label(db_conn):
    _seed_user(db_conn, "cap-u5")
    kwargs = dict(
        run_id="run-5",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[{"label": "linkedin", "job_count": 1}],
        parse_failures=[],
    )

    capture.record_run(db_conn, "cap-u5", **kwargs)
    # A second call with the SAME (user_id, run_id, sender_label) must not
    # duplicate rows or raise -- ON CONFLICT DO NOTHING.
    capture.record_run(db_conn, "cap-u5", **kwargs)

    n = db_conn.execute(
        "SELECT count(*) AS n FROM email_parse_log_sender "
        "WHERE user_id = %s AND run_id = %s AND sender_label = 'linkedin'",
        ("cap-u5", "run-5"),
    ).fetchone()["n"]
    assert n == 1


def test_record_run_scopes_rows_to_user_id(db_conn):
    """The same run_id used by two different tenants writes two disjoint
    row sets -- the widened UNIQUE(user_id, run_id, sender_label) (m0026)
    is what makes that safe."""
    _seed_user(db_conn, "cap-u6a")
    _seed_user(db_conn, "cap-u6b")
    kwargs = dict(
        run_id="shared-run",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[{"label": "linkedin", "job_count": 1}],
        parse_failures=[],
    )

    capture.record_run(db_conn, "cap-u6a", **kwargs)
    capture.record_run(db_conn, "cap-u6b", **kwargs)

    n = db_conn.execute(
        "SELECT count(*) AS n FROM email_parse_log_sender WHERE run_id = 'shared-run' "
        "AND sender_label = 'linkedin'"
    ).fetchone()["n"]
    assert n == 2


def test_record_run_never_raises_on_internal_failure(db_conn):
    """Matches the private function's contract exactly: observability must
    not break ingestion. A malformed extraction_records entry (missing the
    required "label" key) must be swallowed, not propagated."""
    _seed_user(db_conn, "cap-u7")

    capture.record_run(
        db_conn,
        "cap-u7",
        run_id="run-7",
        processed_at=datetime(2026, 7, 17, tzinfo=UTC),
        extraction_records=[{"job_count": 1}],  # missing "label" -- KeyError internally
        parse_failures=[],
    )  # must not raise
