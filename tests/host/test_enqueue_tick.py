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


import contextlib


@contextlib.contextmanager
def _fake_conn_ctx():
    yield object()


def test_enqueue_due_scans_defers_one_scan_per_company_with_queueing_lock(monkeypatch):
    from procrastinate import testing

    from jobcannon.host import scan_tasks, tasks

    monkeypatch.setattr(
        scan_tasks, "_due_company_names", lambda conn, *, interval_hours: ["Acme", "Globex"]
    )
    monkeypatch.setattr(tasks, "_tick_connection", _fake_conn_ctx)
    with tasks.app.replace_connector(testing.InMemoryConnector()) as app:
        result = tasks.enqueue_due_scans(0)
        jobs = list(app.connector.jobs.values())
        scan_jobs = [j for j in jobs if j["task_name"] == "jobcannon.host.tasks.scan"]
        assert {j["args"]["company_name"] for j in scan_jobs} == {"Acme", "Globex"}
        assert {j["queueing_lock"] for j in scan_jobs} == {"scan:Acme", "scan:Globex"}
        assert result == {"enqueued": 2, "already_enqueued": 0}


def test_enqueue_due_scans_tolerates_already_enqueued(monkeypatch):
    from procrastinate import testing

    from jobcannon.host import scan_tasks, tasks

    monkeypatch.setattr(
        scan_tasks, "_due_company_names", lambda conn, *, interval_hours: ["Acme"]
    )
    monkeypatch.setattr(tasks, "_tick_connection", _fake_conn_ctx)
    with tasks.app.replace_connector(testing.InMemoryConnector()) as app:
        tasks.enqueue_due_scans(0)
        result = tasks.enqueue_due_scans(0)  # same lock still todo -> AlreadyEnqueued path
        assert result == {"enqueued": 0, "already_enqueued": 1}
        scan_jobs = [
            j for j in app.connector.jobs.values()
            if j["task_name"] == "jobcannon.host.tasks.scan"
        ]
        assert len(scan_jobs) == 1
