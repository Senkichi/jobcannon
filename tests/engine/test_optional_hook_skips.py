"""Optional-hook skip-branch coverage — review finding FIX C, PR #6.

Three optional ScanServices hooks had their None-default skip branches
never exercised with the hook actually unset — existing tests either mock
the whole containing function away (see test_run_ats_scan_wiring.py's
_patch_all_phases) or wire the hook non-None (test_ats_scanner_run.py's
Phase C test always supplies find_careers_url/scrape_careers_page):

- ``score_and_persist_job`` / ``enrich_job`` in
  ``_run.py::_score_new_ats_jobs`` (~line 1379/1396)
- ``run_homepage_discovery`` in ``_run.py::_run_homepage_discovery_phase``
  (~line 1342)
- ``find_careers_url`` / ``scrape_careers_page`` in
  ``_run_html.py::_run_html_fallback_scan`` (~line 53)

Each test below drives the real function with the hook(s) at their unset
(None) default and asserts a clean skip: no exception, scan proceeds, no
partial/inconsistent state written.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._run import _run_homepage_discovery_phase, _score_new_ats_jobs
from jobcannon.engine.ats_scanner._run_html import _run_html_fallback_scan

from tests.engine.helpers.ats_scan_services import create_scan_schema, make_scan_services


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    create_scan_schema(conn)
    conn.close()
    return path


# ---------------------------------------------------------------------------
# _run_homepage_discovery_phase — run_homepage_discovery unset.
# ---------------------------------------------------------------------------


def test_run_homepage_discovery_phase_skips_when_hook_unset(db_path):
    services.set_services(make_scan_services(db_path))
    try:
        # Mirrors the real call site's pre-seeded summary shape.
        summary = {"companies_scanned": 0, "jobs_discovered": 0, "homepages_discovered": 0}
        _run_homepage_discovery_phase(db_path, {}, summary, company_names=None)
    finally:
        services.clear_services()

    # Early return leaves the pre-seeded value untouched — no crash, no
    # partial write, no KeyError from a downstream reader of this key.
    assert summary["homepages_discovered"] == 0


# ---------------------------------------------------------------------------
# _score_new_ats_jobs — score_and_persist_job unset (whole-function skip)
# and enrich_job unset (scoring still proceeds without enrichment).
# ---------------------------------------------------------------------------


def _seed_job(conn: sqlite3.Connection, dedup_key: str, **cols) -> None:
    fields = {"dedup_key": dedup_key, "title": "Engineer", "company": "Acme", **cols}
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO jobs ({', '.join(fields)}) VALUES ({placeholders})",
        list(fields.values()),
    )
    conn.commit()


def test_score_new_ats_jobs_skips_when_score_and_persist_job_unset(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_job(conn, "dk1")

    services.set_services(make_scan_services(db_path))
    try:
        summary = {"classified_apply": 0, "classified_consider": 0, "errors": []}
        # score_and_persist_job defaults to None on a fresh ScanServices bundle.
        _score_new_ats_jobs(conn, {}, ["dk1"], summary)
    finally:
        services.clear_services()

    assert summary["classified_apply"] == 0
    assert summary["classified_consider"] == 0
    assert summary["errors"] == []
    # jd_full stays whatever it started as (NULL) — no enrichment or
    # scoring side effect leaked through the skip.
    row = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'dk1'").fetchone()
    assert row["jd_full"] is None
    conn.close()


def test_score_new_ats_jobs_skips_enrichment_but_still_scores_when_enrich_job_unset(db_path):
    """enrich_job unset must NOT block scoring itself — only the enrichment
    sub-step is skipped; score_and_persist_job still runs on the
    un-enriched row."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # No jd_full/salary_min/location -> would normally trigger enrichment
    # if enrich_job were wired.
    _seed_job(conn, "dk1", jd_full=None)

    scored_rows = []

    def fake_score_and_persist_job(job_row, conn, config):
        scored_rows.append(job_row)
        return None  # None result -> _score_new_ats_jobs just `continue`s

    services.set_services(
        make_scan_services(db_path, score_and_persist_job=fake_score_and_persist_job)
        # enrich_job intentionally left at its None default.
    )
    try:
        summary = {"classified_apply": 0, "classified_consider": 0, "errors": []}
        _score_new_ats_jobs(conn, {}, ["dk1"], summary)
    finally:
        services.clear_services()

    assert len(scored_rows) == 1, "score_and_persist_job must still be called"
    assert scored_rows[0]["dedup_key"] == "dk1"
    assert scored_rows[0]["jd_full"] is None, "no enrichment hook was wired, so jd_full stays NULL"
    assert summary["errors"] == []
    conn.close()


# ---------------------------------------------------------------------------
# _run_html_fallback_scan — find_careers_url / scrape_careers_page unset.
# ---------------------------------------------------------------------------


def _seed_html_eligible_company(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_probe_status, homepage_url, scan_enabled,
            careers_crawl_last_at, created_at, updated_at)
           VALUES ('acme', 'Acme', 'miss', 'https://acme.example', 1,
                   NULL, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
    )
    conn.commit()


def test_run_html_fallback_scan_skips_when_find_careers_url_unset(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_html_eligible_company(conn)

    services.set_services(
        make_scan_services(
            db_path,
            scrape_careers_page=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("scrape_careers_page must not be called")
            ),
        )
        # find_careers_url intentionally left at its None default.
    )
    try:
        summary = {"jobs_new": 0, "errors": [], "html_scraped": 0}
        _run_html_fallback_scan(
            conn, db_path, {}, ["Engineer"], [], summary, [], high_score_threshold=999
        )
    finally:
        services.clear_services()

    assert summary == {"jobs_new": 0, "errors": [], "html_scraped": 0}
    conn.close()


def test_run_html_fallback_scan_skips_when_scrape_careers_page_unset(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_html_eligible_company(conn)

    services.set_services(
        make_scan_services(
            db_path,
            find_careers_url=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("find_careers_url must not be called")
            ),
        )
        # scrape_careers_page intentionally left at its None default.
    )
    try:
        summary = {"jobs_new": 0, "errors": [], "html_scraped": 0}
        _run_html_fallback_scan(
            conn, db_path, {}, ["Engineer"], [], summary, [], high_score_threshold=999
        )
    finally:
        services.clear_services()

    assert summary == {"jobs_new": 0, "errors": [], "html_scraped": 0}
    conn.close()
