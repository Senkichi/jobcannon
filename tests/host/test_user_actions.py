"""save_posting / unsave_posting / dismiss_posting / mark_applied
(jobcannon/db/_user_actions.py) — the single writer for `watchlists` and
`pipeline_status`. Rollback-isolated `db_conn` fixture, same shape as
tests/host/test_profiles_dal.py: every write here is undone at test end."""

from __future__ import annotations

import pytest


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def _seed_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (%s, %s, 'jobvite', %s, 'hit') RETURNING id",
        (name, name, name.lower().replace(" ", "-")),
    ).fetchone()["id"]


def _seed_posting(conn, dedup_key, company_id, *, title="Engineer", company="Acme"):
    return conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (dedup_key, company_id, title, company),
    ).fetchone()["id"]


def test_save_is_idempotent_and_writes_one_watchlist_row(db_conn):
    from jobcannon.db._user_actions import save_posting

    _seed_user(db_conn, "u-save")
    company_id = _seed_company(db_conn, "Save Co")
    posting_id = _seed_posting(db_conn, "save-1", company_id)

    save_posting(db_conn, "u-save", posting_id)
    save_posting(db_conn, "u-save", posting_id)  # double-submit

    rows = db_conn.execute(
        "SELECT id FROM watchlists WHERE user_id = %s AND posting_id = %s", ("u-save", posting_id)
    ).fetchall()
    assert len(rows) == 1


def test_dismiss_then_apply_overwrites_status_on_the_shared_row(db_conn):
    from jobcannon.db._user_actions import dismiss_posting, mark_applied

    _seed_user(db_conn, "u-shared-row")
    company_id = _seed_company(db_conn, "Shared Row Co")
    posting_id = _seed_posting(db_conn, "shared-row-1", company_id)

    dismiss_posting(db_conn, "u-shared-row", posting_id)
    row = db_conn.execute(
        "SELECT status, applied_at FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
        ("u-shared-row", posting_id),
    ).fetchone()
    assert row["status"] == "dismissed"
    assert row["applied_at"] is None

    mark_applied(db_conn, "u-shared-row", posting_id)
    row = db_conn.execute(
        "SELECT status, applied_at FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
        ("u-shared-row", posting_id),
    ).fetchone()
    assert row["status"] == "applied"
    applied_at = row["applied_at"]
    assert applied_at is not None

    # One row throughout: PK (user_id, posting_id) means dismiss and apply
    # share it, never a second row.
    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
        ("u-shared-row", posting_id),
    ).fetchone()["n"]
    assert count == 1

    # Dismissing again after an apply overwrites status back to 'dismissed'
    # but must NOT clear the applied_at an earlier apply already wrote (the
    # UPDATE SET clause for a non-apply write never lists applied_at).
    dismiss_posting(db_conn, "u-shared-row", posting_id)
    row = db_conn.execute(
        "SELECT status, applied_at FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
        ("u-shared-row", posting_id),
    ).fetchone()
    assert row["status"] == "dismissed"
    assert row["applied_at"] == applied_at


def test_invalid_status_value_raises_at_the_write_boundary(db_conn):
    from jobcannon.db._user_actions import _set_pipeline_status

    _seed_user(db_conn, "u-invalid-status")
    company_id = _seed_company(db_conn, "Invalid Status Co")
    posting_id = _seed_posting(db_conn, "invalid-status-1", company_id)

    with pytest.raises(ValueError):
        _set_pipeline_status(db_conn, "u-invalid-status", posting_id, "bogus")

    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
        ("u-invalid-status", posting_id),
    ).fetchone()["n"]
    assert count == 0


def test_no_python_wallclock_in_the_writer():
    import pathlib

    src = pathlib.Path("jobcannon/db/_user_actions.py").read_text(encoding="utf-8")
    assert "datetime.now(" not in src
    assert "utcnow(" not in src
