"""Periodic enqueue tick: the due-predicate SQL and the defer/queueing-lock
behavior. The engine re-checks full eligibility (dormancy, retry_after)
inside run_ats_scan — the tick's predicate is deliberately only the cheap
"scan_enabled and not scanned within the interval" approximation, so an
over-eager tick is safe and an under-eager one is the bug class to test."""

import contextlib
import csv
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
    _seed_company(db_conn, "NeverScanned")  # due (NULL)
    _seed_company(db_conn, "StaleCo", last_scanned_at=now - timedelta(hours=9))  # due (9h > 8h)
    _seed_company(db_conn, "FreshCo", last_scanned_at=now - timedelta(hours=1))  # not due
    _seed_company(db_conn, "DisabledCo", scan_enabled=False)  # never due
    names = _due_company_names(db_conn, interval_hours=8)
    assert names == ["NeverScanned", "StaleCo"]  # NULLS FIRST, then oldest-first


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

    monkeypatch.setattr(scan_tasks, "_due_company_names", lambda conn, *, interval_hours: ["Acme"])
    monkeypatch.setattr(tasks, "_tick_connection", _fake_conn_ctx)
    with tasks.app.replace_connector(testing.InMemoryConnector()) as app:
        tasks.enqueue_due_scans(0)
        result = tasks.enqueue_due_scans(0)  # same lock still todo -> AlreadyEnqueued path
        assert result == {"enqueued": 0, "already_enqueued": 1}
        scan_jobs = [
            j for j in app.connector.jobs.values() if j["task_name"] == "jobcannon.host.tasks.scan"
        ]
        assert len(scan_jobs) == 1


def test_preseed_corpus_csv_to_defer_loop(tmp_path):
    """scripts/preseed_corpus.py's CSV->defer loop (OD-10), covered here per
    the plan's test-coverage note: no network, no DB — the CSV read plus
    _enqueue_scans's pure-connector defer loop, against an InMemoryConnector
    swapped in via replace_connector. --verify's HTTP checks are operator
    tooling, not exercised beyond arg parsing."""
    from procrastinate import testing

    from jobcannon.host import tasks
    from scripts import preseed_corpus

    csv_path = tmp_path / "seed_companies.csv"
    csv_path.write_text(
        "name,ats_platform,ats_slug,homepage_url\n"
        "Acme,greenhouse,acme,https://acme.example\n"
        "Globex,lever,globex,https://globex.example\n"
        "Initech,ashby,initech,https://initech.example\n",
        encoding="utf-8",
    )
    rows = preseed_corpus._read_rows(str(csv_path))
    assert len(rows) == 3

    with tasks.app.replace_connector(testing.InMemoryConnector()) as app:
        enqueued, already = preseed_corpus._enqueue_scans(rows)
        assert (enqueued, already) == (3, 0)
        jobs = list(app.connector.jobs.values())
        scan_jobs = [j for j in jobs if j["task_name"] == "jobcannon.host.tasks.scan"]
        assert {j["args"]["company_name"] for j in scan_jobs} == {"Acme", "Globex", "Initech"}
        assert {j["queueing_lock"] for j in scan_jobs} == {
            "scan:Acme",
            "scan:Globex",
            "scan:Initech",
        }

        # Re-running the loop against the same still-todo jobs hits the
        # queueing-lock dedup path, same as the periodic tick's own.
        enqueued2, already2 = preseed_corpus._enqueue_scans(rows)
        assert (enqueued2, already2) == (0, 3)


def test_preseed_corpus_read_rows_roundtrips_csv_header(tmp_path):
    csv_path = tmp_path / "seed_companies.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "ats_platform", "ats_slug", "homepage_url"])
        writer.writerow(["Acme", "greenhouse", "acme", "https://acme.example"])

    from scripts import preseed_corpus

    rows = preseed_corpus._read_rows(str(csv_path))
    assert rows == [
        {
            "name": "Acme",
            "ats_platform": "greenhouse",
            "ats_slug": "acme",
            "homepage_url": "https://acme.example",
        }
    ]


def test_preseed_upsert_skips_row_level_failures(monkeypatch):
    """_upsert_companies' skip contract: a CompanyNameRejectedError OR a
    CompanyUpsertError on one row is logged-and-skipped, never aborts the
    seed — the remaining rows still land and the count reflects only them."""
    import contextlib
    from types import SimpleNamespace

    from jobcannon.db import _companies
    from jobcannon.engine import services
    from scripts import preseed_corpus

    seen: list[str] = []

    def fake_upsert(conn, name, **kwargs):
        seen.append(name)
        if name == "Malformed":
            raise _companies.CompanyNameRejectedError(name, "no_alphanumeric_characters")
        if name == "DbSad":
            raise _companies.CompanyUpsertError(name, RuntimeError("boom"))
        return len(seen)

    @contextlib.contextmanager
    def fake_factory():
        yield SimpleNamespace(commit=lambda: None)

    monkeypatch.setattr(_companies, "upsert_company", fake_upsert)
    monkeypatch.setattr(
        services, "get_services", lambda: SimpleNamespace(connection_factory=fake_factory)
    )

    rows = [
        {"name": "Acme", "ats_platform": "greenhouse", "ats_slug": "acme"},
        {"name": "Malformed", "ats_platform": "lever", "ats_slug": "bad1"},
        {"name": "DbSad", "ats_platform": "lever", "ats_slug": "bad2"},
        {"name": "Globex", "ats_platform": "lever", "ats_slug": "globex"},
    ]
    assert preseed_corpus._upsert_companies(rows) == 2
    assert seen == ["Acme", "Malformed", "DbSad", "Globex"]
