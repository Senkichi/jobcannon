"""Minimal companies/jobs/company_scan_log schema + fake ScanServices for
ats_scanner._run tests.

The engine has no migrations system (host-owned, not ported — see
jobcannon/engine/services.py module docstring). Tests that drive
_scan_one_company_via_ats_api / _upsert_one_ats_api_job / _run_ats_api_scan /
_run_html_fallback_scan against a real on-disk sqlite3 database build their
own minimal schema here instead of running the private repo's
job_finder.web.db_migrate.run_migrations.

create_scan_schema() creates only the columns those functions' SQL literally
references (verified directly against jobcannon/engine/ats_scanner/_run.py
and _run_html.py) — not the full production companies/jobs tables.

make_scan_services() wires a ScanServices bundle whose upsert_job/set_jd_full
do REAL inserts/updates against that schema (not no-op fakes), so the
post-upsert UPDATE statements in _upsert_one_ats_api_job (is_remote/
employment_type/department, ats_refreshed_at) — which read back via a
separate connection — actually have a row to land on. connection_factory
therefore opens a NEW sqlite3 connection to the same on-disk db_path on every
call (mirrors the private repo's db_helpers.standalone_connection(db_path)
semantics), rather than sharing a single connection or using :memory: (which
is unshareable across connections).
"""

from __future__ import annotations

import contextlib
import sqlite3

from jobcannon.engine.services import ScanServices

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    name_raw TEXT,
    ats_platform TEXT,
    ats_slug TEXT,
    ats_probe_status TEXT,
    scan_enabled INTEGER DEFAULT 1,
    last_scanned_at TEXT,
    consecutive_empty_scans INTEGER DEFAULT 0,
    jobs_found_total INTEGER DEFAULT 0,
    retry_after TEXT,
    retry_count INTEGER DEFAULT 0,
    miss_reason TEXT,
    homepage_url TEXT,
    careers_url TEXT,
    careers_crawl_last_at TEXT,
    last_scan_postings_json TEXT,
    last_scan_cached_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE,
    title TEXT,
    company TEXT,
    company_id INTEGER,
    jd_full TEXT,
    comp_data_json TEXT,
    is_remote INTEGER,
    employment_type TEXT,
    department TEXT,
    ats_refreshed_at TEXT,
    sub_scores_json TEXT
);

CREATE TABLE IF NOT EXISTS company_scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    scanned_at TEXT,
    jobs_found INTEGER,
    skipped_title_filter INTEGER,
    error TEXT
);
"""


def create_scan_schema(conn: sqlite3.Connection) -> None:
    """Create the minimal companies/jobs/company_scan_log tables on conn."""
    conn.executescript(_SCHEMA)
    conn.commit()


def _raw_connect(db_path: str, *, synchronous: str = "FULL") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA synchronous = {synchronous}")
    return conn


@contextlib.contextmanager
def open_connection(db_path: str, *, synchronous: str = "FULL"):
    """Context manager that opens a fresh connection to db_path and closes it
    on exit (mirrors the private repo's db_helpers.standalone_connection).

    NOTE: a bare sqlite3.Connection's own __enter__/__exit__ only wraps a
    transaction (commit/rollback) — it does NOT close the connection. Tests
    that do ``with open_connection(path) as conn:`` rely on this wrapper for
    the close(), not on sqlite3.Connection's built-in context-manager
    protocol.
    """
    conn = _raw_connect(db_path, synchronous=synchronous)
    try:
        yield conn
    finally:
        conn.close()


def make_connection_factory(db_path: str):
    """Build a ScanServices.connection_factory bound to db_path.

    Must accept the optional `synchronous` keyword (two scan-worker hot-path
    call sites in _run.py pass synchronous="NORMAL") — see services.py's
    ScanServices.connection_factory docstring.
    """

    @contextlib.contextmanager
    def factory(*, synchronous: str = "FULL"):
        with open_connection(db_path, synchronous=synchronous) as conn:
            yield conn

    return factory


class FakeUpsertResult:
    """Mirrors job_finder.db._jobs.UpsertResult's .kind / .dedup_key shape
    (same contract as tests/engine/test_scan_seam.py's _FakeUpsertResult)."""

    def __init__(self, kind: str, dedup_key: str, unresolved_reasons=None):
        self.kind = kind
        self.dedup_key = dedup_key
        self.unresolved_reasons = unresolved_reasons or []


def make_fake_upsert_job():
    """Real (not no-op) upsert against the jobs table: INSERT on a new
    dedup_key (kind='inserted'), no-op on an existing one (kind='unchanged').

    The first-seen-wins vs. every-sighting distinction that
    _upsert_one_ats_api_job branches on (is_remote/employment_type/department
    vs. ats_refreshed_at) depends on this being a real dedup_key lookup
    against the jobs table, not a fixed canned result.
    """

    def fake_upsert_job(conn, parsed, *, company_id=None, ats_platform=None, **_kw):
        dedup_key = parsed.dedup_key
        existing = conn.execute("SELECT id FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, company_id) VALUES (?, ?, ?, ?)",
                (dedup_key, parsed.title, parsed.company, company_id),
            )
            conn.commit()
            return FakeUpsertResult(kind="inserted", dedup_key=dedup_key)
        conn.commit()
        return FakeUpsertResult(kind="unchanged", dedup_key=dedup_key)

    return fake_upsert_job


def make_fake_set_jd_full():
    def fake_set_jd_full(conn, dedup_key, jd_full, *, source=None):
        conn.execute("UPDATE jobs SET jd_full = ? WHERE dedup_key = ?", (jd_full, dedup_key))
        conn.commit()

    return fake_set_jd_full


def make_scan_services(db_path: str, **overrides) -> ScanServices:
    """Build a ScanServices bundle whose required fields do real work against
    db_path; **overrides layers in optional hooks (e.g. find_careers_url /
    scrape_careers_page for Phase C tests)."""
    kwargs: dict = dict(
        connection_factory=make_connection_factory(db_path),
        upsert_job=make_fake_upsert_job(),
        set_jd_full=make_fake_set_jd_full(),
        upsert_company=lambda conn, name, *a, **k: 1,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )
    kwargs.update(overrides)
    return ScanServices(**kwargs)
