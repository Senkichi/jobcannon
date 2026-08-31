"""count_saved_postings / count_pipeline_statuses (jobcannon/db/_user_actions.py)
— Spec 2's stats-strip COUNT primitives. Rollback-isolated `db_conn`, seed
helpers copied from tests/host/test_user_actions.py (same table shapes)."""

from __future__ import annotations


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


def test_counts_are_zero_for_a_user_with_no_rows(db_conn):
    """Absence is the neutral state (spec: 'row-absence as the neutral
    state'): no watchlists row, no pipeline_status row -> every count is 0
    and every status key is still present."""
    from jobcannon.db._user_actions import count_pipeline_statuses, count_saved_postings

    _seed_user(db_conn, "cnt-empty")

    assert count_saved_postings(db_conn, "cnt-empty") == 0
    assert count_pipeline_statuses(db_conn, "cnt-empty") == {"dismissed": 0, "applied": 0}


def test_counts_follow_save_dismiss_apply_and_unsave(db_conn):
    """seed -> count -> act -> recount, through the module's own writers so
    the counts are pinned to what those writers actually store."""
    from jobcannon.db._user_actions import (
        count_pipeline_statuses,
        count_saved_postings,
        dismiss_posting,
        mark_applied,
        save_posting,
        unsave_posting,
    )

    _seed_user(db_conn, "cnt-flow")
    company_id = _seed_company(db_conn, "Count Co")
    p1 = _seed_posting(db_conn, "cnt-1", company_id)
    p2 = _seed_posting(db_conn, "cnt-2", company_id)
    p3 = _seed_posting(db_conn, "cnt-3", company_id)

    save_posting(db_conn, "cnt-flow", p1)
    save_posting(db_conn, "cnt-flow", p2)
    save_posting(db_conn, "cnt-flow", p2)  # idempotent double-save: still one row
    assert count_saved_postings(db_conn, "cnt-flow") == 2

    dismiss_posting(db_conn, "cnt-flow", p1)
    dismiss_posting(db_conn, "cnt-flow", p2)
    mark_applied(db_conn, "cnt-flow", p3)
    assert count_pipeline_statuses(db_conn, "cnt-flow") == {"dismissed": 2, "applied": 1}

    # dismiss -> apply shares the (user_id, posting_id) row: the status
    # moves between buckets, the total stays 3.
    mark_applied(db_conn, "cnt-flow", p1)
    assert count_pipeline_statuses(db_conn, "cnt-flow") == {"dismissed": 1, "applied": 2}

    unsave_posting(db_conn, "cnt-flow", p1)
    assert count_saved_postings(db_conn, "cnt-flow") == 1


def test_counts_are_scoped_to_the_requested_user(db_conn):
    from jobcannon.db._user_actions import (
        count_pipeline_statuses,
        count_saved_postings,
        dismiss_posting,
        save_posting,
    )

    _seed_user(db_conn, "cnt-a")
    _seed_user(db_conn, "cnt-b")
    company_id = _seed_company(db_conn, "Scope Co")
    posting_id = _seed_posting(db_conn, "cnt-scope-1", company_id)
    save_posting(db_conn, "cnt-a", posting_id)
    dismiss_posting(db_conn, "cnt-a", posting_id)

    assert count_saved_postings(db_conn, "cnt-b") == 0
    assert count_pipeline_statuses(db_conn, "cnt-b") == {"dismissed": 0, "applied": 0}
    assert count_saved_postings(db_conn, "cnt-a") == 1
    assert count_pipeline_statuses(db_conn, "cnt-a") == {"dismissed": 1, "applied": 0}


def test_company_watches_do_not_count_as_saved_postings(db_conn):
    """watchlists holds EITHER a posting_id OR a company_id (m0001 CHECK).
    Only posting saves are 'Saved' on the profile strip."""
    from jobcannon.db._user_actions import count_saved_postings

    _seed_user(db_conn, "cnt-company")
    company_id = _seed_company(db_conn, "Watched Co")
    db_conn.execute(
        "INSERT INTO watchlists (user_id, company_id) VALUES (%s, %s)",
        ("cnt-company", company_id),
    )

    assert count_saved_postings(db_conn, "cnt-company") == 0
