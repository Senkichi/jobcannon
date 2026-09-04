# PORTED from tests/test_careers_crawler_penalty_box.py @ 46d5a2a1f27179d075efc5572efeded3ba2a0266 (private job-cannon). Ledger L-0528.
# PORT-SEAM: same connection_factory / crawl_careers_batch(config) /
# stub_sync_playwright seam as tests/engine/test_ats_link_discovery.py
# (L-0527, this same carry pass). Column rename (L-0461): companies.
# scan_enabled -> careers_scan_enabled, applied to the hand-rolled SQL
# literal in test_a1_benched_count and to _insert_company's bind.
# record_scan_outcome is an optional host-injected ScanServices field
# (L-0465), default None; careers_crawler._persistence silently skips the
# company_scan_log write when it is unwired, which would starve every
# strike/bench assertion below. The shared crawler_db_path fixture wires the
# real jobcannon.engine.ats_scanner._scan_log.record_scan_outcome writer,
# matching the precedent in tests/engine/test_careers_crawler_persistence.py.
#
# Dropped (5 of 39, all of TestWriterCompleteness): a private-tree-shaped
# source-completeness meta-guard -- walks job_finder/ counting
# record_scan_outcome(...) call sites against a hardcoded expected count
# (10), asserts a single raw INSERT INTO company_scan_log lives at a
# private-tree path (db/_scan_log.py), and checks per-call source literals
# against private module paths (web/careers_crawler/_persistence.py,
# web/ats_scanner/). Every path and count is specific to the private module
# layout; re-deriving the public jobcannon/ tree's equivalent guard is a
# fresh, separately-scoped investigation of the whole source tree, not a
# mechanical port. See this PR's body.
from __future__ import annotations

# PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
import sqlite3
from datetime import UTC, datetime, timedelta

# PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.careers_crawler import crawl_careers_batch
from jobcannon.engine.careers_crawler._bench_predicate import (
    BENCH_CLEAN_FAILURE_REASONS,
    BENCH_CRAWLER_SOURCE,
    BENCH_PREDICATE_SQL,  # PORT-SEAM: import list regrouped for the public engine package layout
    BENCH_STRIKE_DECAY_DAYS,
    BENCH_STRIKE_THRESHOLD,
    BENCH_UNATTRIBUTED_ZERO_HIT_REASON,
    build_bench_predicate_sql,
    is_company_benched,
    resolve_bench_decay_days,
)
from jobcannon.engine.json_utils import utc_now_iso

from jobcannon.engine.ats_scanner._scan_log import record_scan_outcome
from tests.engine.helpers.ats_scan_services import create_scan_schema, make_scan_services

# ---------------------------------------------------------------------------
# DB fixtures / harness -- same crawler_db_path + stub_sync_playwright shape
# established in tests/engine/test_ats_link_discovery.py (L-0527, this same
# carry pass). PORT-SEAM: private's bare `sqlite3.connect(migrated_db_path)` /
# `crawl_careers_batch(db_path, config)` calls become `svc.connection_factory()`
# / `crawl_careers_batch(config)` throughout (L-0461/L-0463's zero-arg seam).
# ---------------------------------------------------------------------------


@pytest.fixture
def crawler_db_path(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    create_scan_schema(conn)
    conn.execute("ALTER TABLE companies ADD COLUMN careers_scan_enabled INTEGER DEFAULT 1")
    conn.execute("ALTER TABLE companies ADD COLUMN merged_into_id INTEGER")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_crawl_flag_reason TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_api_endpoint TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_nav_recipe TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN ats_link_discovery_last_at TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN ats_evidence_trigger TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN classification TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN location TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN first_seen TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN source TEXT")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN failure_reason TEXT")
    conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
        "source TEXT, jobs_fetched INTEGER, jobs_new INTEGER, jobs_scored INTEGER)"
    )
    conn.commit()
    conn.close()

    # PORT-SEAM: record_scan_outcome is an optional host-injected seam
    # (L-0465) -- unwired, careers_crawler._persistence silently skips the
    # company_scan_log write entirely, which would starve every strike/
    # bench assertion below. Wire the real writer, matching the precedent
    # in tests/engine/test_careers_crawler_persistence.py.
    services.set_services(make_scan_services(str(db_path), record_scan_outcome=record_scan_outcome))
    return str(db_path)


@pytest.fixture
def stub_sync_playwright():
    """Direct module-attribute substitute for the lazy-loaded playwright
    symbol (PEP 562 __getattr__ in jobcannon/engine/careers_crawler/__init__.py).
    See tests/engine/test_ats_link_discovery.py (L-0527) for the full
    rationale -- unittest.mock.patch's get_original() would otherwise trigger
    a real (failing, in this dev venv) playwright import."""
    import jobcannon.engine.careers_crawler as _cc_pkg

    mock_sp = MagicMock()
    _cc_pkg.__dict__["sync_playwright"] = mock_sp
    yield mock_sp
    _cc_pkg.__dict__.pop("sync_playwright", None)


@pytest.fixture(autouse=True)
def _no_op_http_tiers():
    """Neutralize the non-static HTTP tiers (sitemap/probe/embedded-json) and
    the #1931 opportunistic ATS-link landing-page fetch so these penalty-box
    tests exercise only the code path under test.
    PORT-SEAM: private defined this fixture twice (once per class,
    TestPredicateScoping and TestReasonAwareStrikeSemantics) with identical
    bodies; collapsed to one module-level autouse fixture here -- harmless for
    the non-crawl-driven tests in this file (they never reach these patch
    targets) and avoids the literal duplication."""
    with (
        patch("jobcannon.engine.careers_crawler._escalation._try_sitemap_extract", return_value=[]),
        patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[]),
        patch(
            "jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract",
            return_value=[],
        ),
        patch(
            "jobcannon.engine.careers_crawler._escalation._fetch_careers_landing_html",
            return_value=None,
        ),
    ):
        yield


def _insert_company(conn, name, careers_url, probe_status="miss"):
    """Insert a test company and return its id.
    PORT-SEAM: private bound `scan_enabled`; public renamed the column to
    `careers_scan_enabled` (L-0461)."""  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
    conn.execute(
        "INSERT INTO companies (name, name_raw, careers_url, careers_scan_enabled, "  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
        "ats_probe_status, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, ?, datetime('now'), datetime('now'))",
        (name.lower(), name, careers_url, probe_status),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_high_scoring_job(conn, company_id, title="Engineer Role"):
    """Insert a job with an apply classification so the company qualifies for
    the re-discovery lane (lane 1)."""
    dedup_key = f"test-{company_id}-{title.replace(' ', '-').lower()}"
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, "
        "last_seen, classification, company_id) "
        "VALUES (?, ?, ?, 'remote', datetime('now'), datetime('now'), 'apply', ?)",
        (dedup_key, title, "test", company_id),
    )


def _insert_crawler_scan_row(
    conn: sqlite3.Connection,
    company_id: int,
    jobs_matched: int = 0,
    failure_reason: str | None = None,
    scanned_at: str | None = None,
) -> None:
    """Insert one ``source = 'careers_crawler'`` scan_log row."""
    scanned_at_expr = scanned_at or "datetime('now')"
    conn.execute(
        "INSERT INTO company_scan_log "
        "(company_id, scanned_at, jobs_found, jobs_matched, source, failure_reason) "
        f"VALUES (?, {scanned_at_expr}, 0, ?, 'careers_crawler', ?)",
        (company_id, jobs_matched, failure_reason),
    )


# ---------------------------------------------------------------------------
# 4.2 -- Predicate scoping guard (through crawl_careers_batch)
# ---------------------------------------------------------------------------


# PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
class TestPredicateScoping:
    """The penalty-box predicate must scope on ``source = 'careers_crawler'``.

    Exercised through ``crawl_careers_batch()`` -- not a hand-copied SQL
    fragment -- so the test goes red if the real predicate is never changed.
    """  # PORT-SEAM: em-dash prose normalized to ASCII double-hyphen; dropped a stale "(W1 step 4.2)" reference (W1 already shipped)

    def test_ats_rows_do_not_bench(
        self, crawler_db_path, stub_sync_playwright
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """A company with 5 ATS-origin rows and 0 crawler rows is NOT benched
        (F1: ATS rows must not poison the strike counter)."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "AtsOnly", "https://ats-only.example.com/careers")
            _insert_high_scoring_job(conn, cid)
            for _ in range(BENCH_STRIKE_THRESHOLD):
                conn.execute(
                    "INSERT INTO company_scan_log "
                    "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                    "VALUES (?, datetime('now'), 3, NULL, 'ats_scanner')",
                    (cid,),
                )
            conn.commit()
        finally:
            conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        stub_sync_playwright.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        stub_sync_playwright.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "jobcannon.engine.careers_crawler._escalation._try_static_extract"
        ) as mock_static:
            mock_static.return_value = []
            result = crawl_careers_batch(
                {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
            )
            assert result["companies_crawled"] == 1
            mock_static.assert_called_once()

    def test_crawler_zero_hit_rows_bench(
        self, crawler_db_path, stub_sync_playwright
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """A company with 5 crawler-origin zero-hit rows IS benched."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "BenchCo", "https://benchco.example.com/careers")
            _insert_high_scoring_job(conn, cid)
            for _ in range(BENCH_STRIKE_THRESHOLD):
                conn.execute(
                    "INSERT INTO company_scan_log "
                    "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                    "VALUES (?, datetime('now'), 0, 0, 'careers_crawler')",
                    (cid,),
                )
            conn.commit()
        finally:
            conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        stub_sync_playwright.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        stub_sync_playwright.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "jobcannon.engine.careers_crawler._escalation._try_static_extract"
        ) as mock_static:
            result = crawl_careers_batch(
                {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
            )
            assert result["companies_crawled"] == 0
            mock_static.assert_not_called()

    def test_is_company_benched_ats_only(
        self, crawler_db_path
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """The Python helper ``is_company_benched`` agrees: ATS-only rows do
        not bench. This is the heal-gate path."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "HealAts", "https://heal-ats.example.com/careers")
            for _ in range(BENCH_STRIKE_THRESHOLD):
                conn.execute(
                    "INSERT INTO company_scan_log "
                    "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                    "VALUES (?, datetime('now'), 3, NULL, 'ats_scanner')",
                    (cid,),
                )
            conn.commit()
            assert not is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_is_company_benched_crawler_zero_hit(
        self, crawler_db_path
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """The Python helper ``is_company_benched`` agrees: 5 crawler-origin
        zero-hit rows do bench."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "HealBench", "https://heal-bench.example.com/careers")
            for _ in range(BENCH_STRIKE_THRESHOLD):
                conn.execute(
                    "INSERT INTO company_scan_log "
                    "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                    "VALUES (?, datetime('now'), 0, 0, 'careers_crawler')",
                    (cid,),
                )
            conn.commit()
            assert is_company_benched(conn, cid)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4.3 -- A1 / A3 / A4 fixture assertions (exact equality, fixture DB only)  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
# ---------------------------------------------------------------------------


def _seed_penalty_fixture(db_path: str) -> dict[str, int]:
    """Seed a fixture DB with a known set of companies for A1/A3/A4 checks."""  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
    conn = sqlite3.connect(db_path)
    try:
        labels: dict[str, int] = {}

        def _add(label, name, url):
            conn.execute(
                "INSERT INTO companies (name, name_raw, careers_url, careers_scan_enabled, "  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
                "ats_probe_status, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 'miss', datetime('now'), datetime('now'))",
                (name, name, url),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            labels[label] = cid
            return cid

        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        cid = _add("benched_crawler", "benched-crawler", "https://bc.example.com/careers")
        _insert_high_scoring_job(conn, cid)
        for _ in range(BENCH_STRIKE_THRESHOLD):
            conn.execute(
                "INSERT INTO company_scan_log "
                "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                "VALUES (?, datetime('now'), 0, 0, 'careers_crawler')",
                (cid,),
            )

        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        cid = _add("ats_only", "ats-only", "https://ao.example.com/careers")
        _insert_high_scoring_job(conn, cid)
        for _ in range(BENCH_STRIKE_THRESHOLD):
            conn.execute(
                "INSERT INTO company_scan_log "
                "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                "VALUES (?, datetime('now'), 3, NULL, 'ats_scanner')",
                (cid,),
            )

        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        cid = _add("lane1_eligible", "lane1", "https://l1.example.com/careers")
        _insert_high_scoring_job(conn, cid)

        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        _add("lane2_eligible", "lane2", "https://l2.example.com/careers")

        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        cid = _add("ats_only_lane2", "ats-lane2", "https://al2.example.com/careers")
        for _ in range(BENCH_STRIKE_THRESHOLD):
            conn.execute(
                "INSERT INTO company_scan_log "
                "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                "VALUES (?, datetime('now'), 3, NULL, 'ats_scanner')",
                (cid,),
            )

        conn.commit()
    finally:
        conn.close()
    return labels


class TestAcceptanceCriteria:
    """A1 / A3 / A4 exact-equality assertions against a fixture DB."""  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header

    @pytest.fixture
    def fixture_db(self, crawler_db_path):
        labels = _seed_penalty_fixture(crawler_db_path)
        return (
            crawler_db_path,
            labels,
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    def test_a1_benched_count(self, fixture_db):
        """A1: exactly 1 company matches the penalty predicate (benched on
        crawler rows). The ATS-only companies are NOT benched after W1.
        PORT-SEAM: private's hand-rolled SQL read `c.scan_enabled`; public
        renamed the column to `c.careers_scan_enabled` (L-0461)."""  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
        db_path, labels = fixture_db
        conn = sqlite3.connect(db_path)
        try:
            benched = [
                row[0]
                for row in conn.execute(
                    "SELECT c.id FROM companies c "
                    f"WHERE c.careers_url IS NOT NULL AND c.careers_scan_enabled = 1 "  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
                    f"AND c.ats_probe_status IS NOT 'hit' "
                    f"AND c.careers_crawl_flag_reason IS NULL "
                    f"AND NOT EXISTS ("
                    f"SELECT 1 FROM ("
                    f"SELECT COUNT(*) AS total, "
                    f"SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits "
                    f"FROM company_scan_log "
                    f"WHERE company_id = c.id AND source = '{BENCH_CRAWLER_SOURCE}'"
                    f") s WHERE s.total >= {BENCH_STRIKE_THRESHOLD} AND s.hits = 0"
                    f")"
                ).fetchall()
            ]
            # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
            eligible = set(benched)
            assert labels["benched_crawler"] not in eligible
            assert labels["ats_only"] in eligible
            assert labels["lane1_eligible"] in eligible
            assert labels["lane2_eligible"] in eligible
            assert labels["ats_only_lane2"] in eligible
        finally:
            conn.close()

    def test_a3_zero_lane_eligible_benched_on_zero_crawler_attempts(self, fixture_db):
        """A3: zero companies that are lane-eligible AND benched while having
        zero crawler attempts."""  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        db_path, labels = fixture_db
        conn = sqlite3.connect(db_path)
        try:
            # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
            benched_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT c.id FROM companies c "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM ("
                    "    SELECT COUNT(*) AS total, "
                    "    SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits "
                    "    FROM company_scan_log "
                    f"    WHERE company_id = c.id AND source = '{BENCH_CRAWLER_SOURCE}'"
                    f"  ) s WHERE s.total >= {BENCH_STRIKE_THRESHOLD} AND s.hits = 0"
                    ") = 0"
                ).fetchall()
            ]
            for bid in benched_ids:
                crawler_attempts = conn.execute(
                    "SELECT COUNT(*) FROM company_scan_log "
                    f"WHERE company_id = ? AND source = '{BENCH_CRAWLER_SOURCE}'",
                    (bid,),
                ).fetchone()[0]
                assert crawler_attempts > 0, (
                    f"Company {bid} is benched with zero crawler attempts (A3 violation)"
                )
        finally:
            conn.close()

    def test_a4_predicate_uses_source_column(self, fixture_db):
        """A4: the penalty predicate reads an explicit ``source`` column, not
        a nullable-column proxy."""  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        db_path, _ = fixture_db
        conn = sqlite3.connect(db_path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(company_scan_log)")]
            assert "source" in cols, "company_scan_log must have a source column"
        finally:
            conn.close()
        # PORT-SEAM: import path updated for the public engine package layout (see file header)
        assert "source" in BENCH_PREDICATE_SQL, (
            "BENCH_PREDICATE_SQL must scope on the source column (A4, D2)"
        )


# ---------------------------------------------------------------------------
# W4 -- reason-aware strike semantics (#1725)
# ---------------------------------------------------------------------------  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header


class TestReasonAwareStrikeSemantics:
    """W4 (#1725): a strike counts only when the failure reason indicates a
    broken attempt, not a clean "navigated fine, no roles matched."""

    def test_failure_reason_column_exists(self, crawler_db_path):
        """company_scan_log has a failure_reason column -- the prerequisite
        for W4's reason-aware strike counter."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(company_scan_log)")]
            assert "failure_reason" in cols, (
                "company_scan_log must have a failure_reason column (W2, #1725)"
            )
        finally:
            conn.close()

    def test_predicate_sql_references_failure_reason(self):
        # PORT-SEAM: import path updated for the public engine package layout (see file header)
        assert "failure_reason" in BENCH_PREDICATE_SQL, (
            "BENCH_PREDICATE_SQL must key strikes on failure_reason (W4, #1725)"
        )

    def test_clean_reason_set_contains_no_title_match(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        assert "no_title_match" in BENCH_CLEAN_FAILURE_REASONS

    def test_is_company_benched_clean_no_title_match_does_not_bench(
        self, crawler_db_path
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """The core W4 fix: 5 crawler-origin zero-hit rows carrying a clean
        ``no_title_match`` reason do NOT bench a company."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "CleanCo", "https://cleanco.example.com/careers")
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="no_title_match")
            conn.commit()
            assert not is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_is_company_benched_broken_reason_benches(
        self, crawler_db_path
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """5 crawler-origin zero-hit rows carrying a broken reason
        (``zero_jobs``) DO bench."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "BrokenCo", "https://brokenco.example.com/careers")
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs")
            conn.commit()
            assert is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_is_company_benched_null_failure_reason_benches(self, crawler_db_path):
        """NULL failure_reason (legacy rows, unattributed zero-hits) still
        strikes -- conservative, preserves pre-W4 behaviour."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "LegacyCo", "https://legacyco.example.com/careers")
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason=None)
            conn.commit()
            assert is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_is_company_benched_mixed_below_threshold_does_not_bench(self, crawler_db_path):
        """3 broken + 2 clean zero-hit rows -> only 3 strikes, below the
        threshold -> NOT benched."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "MixedCo", "https://mixedco.example.com/careers")
            for _ in range(3):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs")
            for _ in range(2):
                _insert_crawler_scan_row(conn, cid, failure_reason="no_title_match")
            conn.commit()
            assert not is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_is_company_benched_mixed_at_threshold_benches(self, crawler_db_path):
        """5 broken + 5 clean zero-hit rows -> 5 strikes -> benched."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "MostlyBroken", "https://mostlybroken.example.com/careers")
            for _ in range(5):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs")
            for _ in range(5):
                _insert_crawler_scan_row(conn, cid, failure_reason="no_title_match")
            conn.commit()
            assert is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_is_company_benched_hit_clears_despite_clean_rows(self, crawler_db_path):
        """A single successful scan still clears benching even when many
        clean no-title-match rows are present."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "RevivalCo", "https://revivalco.example.com/careers")
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="no_title_match")
            # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
            conn.execute(
                "INSERT INTO company_scan_log "
                "(company_id, scanned_at, jobs_found, jobs_matched, source) "
                "VALUES (?, datetime('now'), 1, 2, 'careers_crawler')",
                (cid,),
            )
            conn.commit()
            assert not is_company_benched(conn, cid)
        finally:
            conn.close()

    # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
    def test_clean_no_title_match_rows_do_not_bench_via_crawl(
        self,
        crawler_db_path,
        stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    ):
        """The SQL fragment (exercised through crawl_careers_batch) agrees
        with the Python helper: 5 clean no_title_match zero-hit rows do NOT
        exclude the company from either crawl lane."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "CleanCrawl", "https://cleancrawl.example.com/careers")
            _insert_high_scoring_job(conn, cid)
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="no_title_match")
            conn.commit()
        finally:
            conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        stub_sync_playwright.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        stub_sync_playwright.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "jobcannon.engine.careers_crawler._escalation._try_static_extract"
        ) as mock_static:
            result = crawl_careers_batch(
                {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
            )
            assert result["companies_crawled"] == 1
            mock_static.assert_called_once()

    def test_broken_reason_rows_bench_via_crawl(
        self, crawler_db_path, stub_sync_playwright
    ):  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        """The SQL fragment agrees: 5 broken-reason zero-hit rows DO exclude
        the company from crawling."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "BrokenCrawl", "https://brokencrawl.example.com/careers")
            _insert_high_scoring_job(conn, cid)
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs")
            conn.commit()
        finally:
            conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        stub_sync_playwright.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        stub_sync_playwright.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "jobcannon.engine.careers_crawler._escalation._try_static_extract"
        ) as mock_static:
            result = crawl_careers_batch(
                {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
            )
            assert result["companies_crawled"] == 0
            mock_static.assert_not_called()


# ---------------------------------------------------------------------------
# T2.3 (D9) -- strike decay: strikes older than the decay window stop counting  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
# ---------------------------------------------------------------------------


class TestStrikeDecay:
    """A strike older than the configurable decay window no longer counts
    toward the 5-strike threshold."""

    def test_old_strikes_excluded_via_python_helper(self, crawler_db_path):
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "StaleCo", "https://staleco.example.com/careers")
            old_ts = f"datetime('now', '-{BENCH_STRIKE_DECAY_DAYS + 5} days')"
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs", scanned_at=old_ts)
            conn.commit()
            assert not is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_recent_strikes_within_window_still_bench(self, crawler_db_path):
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(
                conn, "ActiveBrokenCo", "https://activebroken.example.com/careers"
            )
            recent_ts = f"datetime('now', '-{max(BENCH_STRIKE_DECAY_DAYS - 5, 1)} days')"
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(
                    conn, cid, failure_reason="zero_jobs", scanned_at=recent_ts
                )
            conn.commit()
            assert is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_mixed_age_below_threshold_after_decay_not_benched(self, crawler_db_path):
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "AgingCo", "https://agingco.example.com/careers")
            old_ts = f"datetime('now', '-{BENCH_STRIKE_DECAY_DAYS + 10} days')"
            for _ in range(2):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs", scanned_at=old_ts)
            for _ in range(3):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs")
            conn.commit()
            assert not is_company_benched(conn, cid)
        finally:
            conn.close()

    def test_custom_decay_days_parameter_respected(self, crawler_db_path):
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(
                conn, "NarrowWindowCo", "https://narrowwindow.example.com/careers"
            )
            ts_10_days_ago = "datetime('now', '-10 days')"
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(
                    conn, cid, failure_reason="zero_jobs", scanned_at=ts_10_days_ago
                )
            conn.commit()
            # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
            assert is_company_benched(conn, cid, decay_days=BENCH_STRIKE_DECAY_DAYS)
            # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
            assert not is_company_benched(conn, cid, decay_days=5)
        finally:
            conn.close()

    def test_sql_fragment_decays_old_strikes(self, crawler_db_path):
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "StaleSqlCo", "https://stalesql.example.com/careers")
            old_ts = f"datetime('now', '-{BENCH_STRIKE_DECAY_DAYS + 5} days')"
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(conn, cid, failure_reason="zero_jobs", scanned_at=old_ts)
            conn.commit()
            predicate, predicate_params = build_bench_predicate_sql()
            row = conn.execute(
                f"SELECT c.id FROM companies c WHERE c.id = ? AND {predicate}",
                (cid, *predicate_params),
            ).fetchone()
            assert row is not None, "stale strikes must not exclude the company"
        finally:
            conn.close()

    def test_sql_fragment_scanned_at_reference(self):
        # PORT-SEAM: import path updated for the public engine package layout (see file header)
        assert "scanned_at" in BENCH_PREDICATE_SQL, (
            "BENCH_PREDICATE_SQL must reference scanned_at for strike decay (T2.3, D9)"
        )

    # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    def test_old_strike_decays_with_real_iso_timestamp_format(self, crawler_db_path):
        """Production writes ``scanned_at`` via ``utc_now_iso()`` -- a
        'T'-separated ISO string with microseconds, not the space-separated
        ``datetime('now', ...)`` SQL form the other decay tests use for
        convenience. Regression guard for the datetime(scanned_at) wrapping
        that normalizes both sides before comparing."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "IsoFormatCo", "https://isoformat.example.com/careers")
            old_dt = datetime.now(UTC) - timedelta(days=BENCH_STRIKE_DECAY_DAYS + 5)
            old_iso = old_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
            assert "T" in utc_now_iso(), "production writer must use 'T' separator"
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(
                    conn, cid, failure_reason="zero_jobs", scanned_at=f"'{old_iso}'"
                )
            conn.commit()
            assert not is_company_benched(conn, cid), (
                "a real ISO-format ('T'-separated) old strike must still decay"
            )
        finally:
            conn.close()


class TestResolveBenchDecayDays:
    """``resolve_bench_decay_days`` is the single place both call sites read
    ``careers_crawl.bench_strike_decay_days`` -- a malformed value must fall
    back to the default rather than silently disabling decay."""  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header

    def test_absent_key_falls_back_to_default(self):
        assert resolve_bench_decay_days({}) == BENCH_STRIKE_DECAY_DAYS
        assert resolve_bench_decay_days({"careers_crawl": {}}) == BENCH_STRIKE_DECAY_DAYS

    def test_valid_value_is_used(self):
        assert resolve_bench_decay_days({"careers_crawl": {"bench_strike_decay_days": 7}}) == 7

    def test_none_value_falls_back_to_default(self):
        assert (
            resolve_bench_decay_days({"careers_crawl": {"bench_strike_decay_days": None}})
            == BENCH_STRIKE_DECAY_DAYS
        )

    def test_non_numeric_string_falls_back_to_default(self):
        assert (
            resolve_bench_decay_days({"careers_crawl": {"bench_strike_decay_days": "not-a-number"}})
            == BENCH_STRIKE_DECAY_DAYS
        )

    def test_negative_value_falls_back_to_default(self):
        assert (
            resolve_bench_decay_days({"careers_crawl": {"bench_strike_decay_days": -1}})
            == BENCH_STRIKE_DECAY_DAYS
        )

    def test_numeric_string_is_coerced(self):
        assert resolve_bench_decay_days({"careers_crawl": {"bench_strike_decay_days": "14"}}) == 14


# ---------------------------------------------------------------------------
# T2.3 (D9) -- strike attribution: unattributed non-ai_nav zero-hits get a  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
# recorded reason instead of NULL, so the box stays auditable.
# ---------------------------------------------------------------------------


class TestStrikeAttribution:
    """NULL-reason non-ai_nav zero-hits must not silently strike with no
    audit trail. T2.3 records ``BENCH_UNATTRIBUTED_ZERO_HIT_REASON`` instead
    of NULL for these rows going forward."""  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header

    def test_unattributed_reason_is_not_a_clean_reason(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        assert BENCH_UNATTRIBUTED_ZERO_HIT_REASON not in BENCH_CLEAN_FAILURE_REASONS

    def test_unattributed_reason_still_benches(self, crawler_db_path):
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(
                conn, "UnattributedCo", "https://unattributed.example.com/careers"
            )
            for _ in range(BENCH_STRIKE_THRESHOLD):
                _insert_crawler_scan_row(
                    conn, cid, failure_reason=BENCH_UNATTRIBUTED_ZERO_HIT_REASON
                )
            conn.commit()
            assert is_company_benched(conn, cid)
        finally:
            conn.close()

    # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
    def test_non_ai_nav_zero_hit_records_reason_not_null(
        self,
        crawler_db_path,
        stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    ):
        """A crawl that zero-hits without ai_nav ever attributing a reason
        (ai_navigation disabled here) writes
        ``BENCH_UNATTRIBUTED_ZERO_HIT_REASON`` on the ``company_scan_log``
        row instead of NULL -- the box stays auditable (T2.3, D9).
        PORT-SEAM: private did not need to mock `_fetch_careers_landing_html`
        (predates #1931); the public port neutralizes it via the
        `_no_op_http_tiers` autouse fixture so the opportunistic ATS-link
        path doesn't interfere with this test's failure_reason assertion."""
        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            cid = _insert_company(conn, "AuditableCo", "https://auditable.example.com/careers")
            _insert_high_scoring_job(conn, cid)
            conn.commit()
        finally:
            conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        stub_sync_playwright.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        stub_sync_playwright.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "jobcannon.engine.careers_crawler._escalation._try_static_extract", return_value=[]
        ):
            crawl_careers_batch(
                {
                    "profile": {"target_titles": ["engineer"], "exclusions": {}},
                    "careers_crawl": {"ai_navigation_enabled": False},
                }
            )

        conn = sqlite3.connect(
            crawler_db_path
        )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
        try:
            row = conn.execute(
                "SELECT failure_reason FROM company_scan_log "
                "WHERE company_id = ? AND source = 'careers_crawler' "
                "ORDER BY id DESC LIMIT 1",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == BENCH_UNATTRIBUTED_ZERO_HIT_REASON
