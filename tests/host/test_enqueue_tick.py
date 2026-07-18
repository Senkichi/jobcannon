"""Periodic enqueue tick: the due-predicate SQL and the defer/queueing-lock
behavior. The engine re-checks full eligibility (dormancy, retry_after)
inside run_ats_scan — the tick's predicate is deliberately only the cheap
"scan_enabled and not scanned within the interval" approximation, so an
over-eager tick is safe and an under-eager one is the bug class to test."""

from datetime import datetime, timedelta, timezone


def _seed_company(conn, name, *, scan_enabled=True, last_scanned_at=None):
    conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status, "
        "scan_enabled, last_scanned_at) VALUES (%s, %s, 'jobvite', %s, 'hit', %s, %s)",
        (name, name, name.lower(), scan_enabled, last_scanned_at),
    )


def test_due_company_names_selects_never_scanned_and_stale_not_fresh_or_disabled(db_conn):
    from jobcannon.host.scan_tasks import _due_company_names

    now = datetime.now(timezone.utc)
    _seed_company(db_conn, "NeverScanned")                                   # due (NULL)
    _seed_company(db_conn, "StaleCo", last_scanned_at=now - timedelta(hours=9))   # due (9h > 8h)
    _seed_company(db_conn, "FreshCo", last_scanned_at=now - timedelta(hours=1))   # not due
    _seed_company(db_conn, "DisabledCo", scan_enabled=False)                 # never due
    names = _due_company_names(db_conn, interval_hours=8)
    assert names == ["NeverScanned", "StaleCo"]  # NULLS FIRST, then oldest-first
