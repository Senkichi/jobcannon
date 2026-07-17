"""Tests for jobcannon.engine.stale_detector — batch archive behavior,
passive-stage staleness marking/clearing, configurable thresholds, and
Section 4's unverifiable-aggregator-listing archival policy.

Ported from job_finder/web/tests/test_stale_detector.py (job-cannon private
repo). The engine has no migrations system (host-owned, not ported — see
CLAUDE.md's engine/host split), so two things did not port:

  - TestIssue1077RetroactiveStamp (all 6 tests) is skipped wholesale: every
    test in it drives the real m205891047 migration helper through a real
    MigrationContext (`job_finder.web.db_migrate.MIGRATIONS` /
    `job_finder.web.migrations.types.MigrationContext`) — neither exists in
    the engine, and there is no migration-numbered fixture to invoke here.
  - Two tests inside TestIssue1077PredicateParity are skipped for the same
    reason (`test_migration_cohort_matches_predicate` and
    `test_full_pipeline_migration_then_decay` both call the migration
    helper as setup). The sibling `test_decay_clock_cohort_matches_predicate`
    does NOT touch the migration and ports fully.

Every other private test function ports with unchanged behavior/assertions
— only the DB wiring changed: the private `migrated_db` fixture (a fully
schema-migrated real on-disk DB) is replaced by the `db` fixture below,
which builds a minimal in-memory schema covering just the jobs/companies/
pipeline_events columns stale_detector.py's SQL actually touches, and wires
it into jobcannon.engine.services.ScanServices.connection_factory so
run_stale_detection's single `with svc.connection_factory() as conn:` call
reuses the very same pre-seeded connection.
"""

import contextlib
import sqlite3
from datetime import datetime, timedelta

import pytest

from jobcannon.engine import services
from jobcannon.engine.source_registry import (
    UNVERIFIABLE_EVIDENCE_CEILING,
    UNVERIFIABLE_EVIDENCE_CONFIRMED,
)
from jobcannon.engine.stale_detector import run_stale_detection

_SCHEMA = """
CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    location TEXT,
    sources TEXT,
    source_urls TEXT,
    source_id TEXT,
    first_seen TEXT,
    last_seen TEXT,
    pipeline_status TEXT,
    is_stale INTEGER DEFAULT 0,
    expiry_status TEXT,
    expiry_checked_at TEXT,
    company_id INTEGER,
    direct_url TEXT,
    direct_url_attempts INTEGER DEFAULT 0,
    careers_checked_at TEXT
);

CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    name_raw TEXT,
    scan_enabled INTEGER DEFAULT 1,
    ats_probe_status TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE pipeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    from_status TEXT,
    to_status TEXT,
    timestamp TEXT,
    source TEXT,
    evidence TEXT
);
"""


@pytest.fixture
def db():
    """(db_path, conn) pair, mirroring the private repo's `migrated_db`
    fixture shape closely enough that ported test bodies only need
    `migrated_db` renamed to `db`.

    run_stale_detection opens exactly ONE connection via
    svc.connection_factory() and reuses it for the whole run (mark/clear/
    archive passes + the Section 4 sweep all share it), so the fake
    factory below yields the SAME pre-built, pre-seeded connection every
    time rather than opening a fresh one — and does not close it, so test
    bodies can keep querying it after run_stale_detection() returns.
    `db_path` is an arbitrary string; the fake factory ignores it.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()

    @contextlib.contextmanager
    def factory(*, synchronous="FULL"):
        yield conn

    services.set_services(
        services.ScanServices(
            connection_factory=factory,
            upsert_job=lambda *a, **k: None,
            set_jd_full=lambda *a, **k: None,
            upsert_company=lambda *a, **k: None,
            get_secret=lambda name, *, config=None: None,
            config={},
            jd_storage_max_chars=100_000,
        )
    )
    try:
        yield "fake.db", conn
    finally:
        services.clear_services()
        conn.close()


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    pipeline_status: str,
    last_seen: str,
    expiry_status: str | None = None,
    sources: str = "[]",
    source_urls: str = "[]",
    expiry_checked_at: str | None = None,
    is_stale: int = 0,
) -> None:
    """Insert a minimal job row for testing."""
    conn.execute(
        """INSERT INTO jobs
           (dedup_key, title, company, location, sources, source_urls, source_id,
            first_seen, last_seen, pipeline_status, is_stale, expiry_status,
            expiry_checked_at)
           VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Test Job",
            "Test Co",
            "Remote",
            sources,
            source_urls,
            "2025-01-01",
            last_seen,
            pipeline_status,
            is_stale,
            expiry_status,
            expiry_checked_at,
        ),
    )
    conn.commit()


def _days_ago(n: int) -> str:
    """Return ISO datetime string for n days ago."""
    return (datetime.now() - timedelta(days=n)).isoformat()


class TestBatchArchive:
    """Test batch archive path in run_stale_detection()."""

    def test_batch_archive(self, db):
        """3 stale discovered jobs → all 3 archived, archived count = 3."""
        path, conn = db
        for i in range(3):
            _insert_job(conn, f"job{i}", "discovered", _days_ago(35))

        result = run_stale_detection(path)

        assert result["archived"] == 3

        rows = conn.execute("SELECT pipeline_status FROM jobs ORDER BY dedup_key").fetchall()
        assert all(r["pipeline_status"] == "archived" for r in rows)

    def test_batch_archive_pipeline_events(self, db):
        """3 stale discovered jobs → pipeline_events has 3 rows with correct fields."""
        path, conn = db
        for i in range(3):
            _insert_job(conn, f"job{i}", "discovered", _days_ago(35))

        run_stale_detection(path)

        events = conn.execute(
            "SELECT job_id, from_status, to_status, source, evidence "
            "FROM pipeline_events ORDER BY job_id"
        ).fetchall()
        assert len(events) == 3
        for event in events:
            assert event["from_status"] == "discovered"
            assert event["to_status"] == "archived"
            assert event["source"] == "stale_detector"
            assert event["evidence"] == "not_seen_30_days"

    def test_batch_archive_mixed_statuses(self, db):
        """2 discovered + 1 reviewing, all 35 days stale → all 3 archived with correct from_status."""
        path, conn = db
        _insert_job(conn, "job0", "discovered", _days_ago(35))
        _insert_job(conn, "job1", "discovered", _days_ago(35))
        _insert_job(conn, "job2", "reviewing", _days_ago(35))

        result = run_stale_detection(path)

        assert result["archived"] == 3

        events = conn.execute(
            "SELECT job_id, from_status FROM pipeline_events ORDER BY job_id"
        ).fetchall()
        assert len(events) == 3
        from_statuses = {e["job_id"]: e["from_status"] for e in events}
        assert from_statuses["job0"] == "discovered"
        assert from_statuses["job1"] == "discovered"
        assert from_statuses["job2"] == "reviewing"

    def test_batch_archive_no_candidates(self, db):
        """Jobs in active stages (applied) are never auto-archived."""
        path, conn = db
        _insert_job(conn, "job_applied", "applied", _days_ago(35))
        _insert_job(conn, "job_phone", "phone_screen", _days_ago(35))

        result = run_stale_detection(path)

        assert result["archived"] == 0

        # pipeline_events should be empty (no archive transitions)
        events = conn.execute("SELECT * FROM pipeline_events").fetchall()
        assert len(events) == 0

    def test_batch_archive_empty(self, db):
        """Empty DB → archived=0, no errors."""
        path, conn = db

        result = run_stale_detection(path)

        assert result["archived"] == 0
        assert result["stale_marked"] == 0
        assert result["stale_cleared"] == 0

    def test_stale_marking_unchanged(self, db):
        """Job seen 15 days ago (stale but not archive candidate) → stale_marked=1."""
        path, conn = db
        _insert_job(conn, "job_stale", "discovered", _days_ago(15))

        result = run_stale_detection(path)

        assert result["stale_marked"] == 1
        assert result["archived"] == 0

        row = conn.execute(
            "SELECT is_stale, pipeline_status FROM jobs WHERE dedup_key = 'job_stale'"
        ).fetchone()
        assert row["is_stale"] == 1
        assert row["pipeline_status"] == "discovered"  # Not archived, just stale


class TestPassiveStageScoping:
    """is_stale is only meaningful pre-application: marked on passive stages,
    cleared everywhere else."""

    def test_active_stage_jobs_never_marked_stale(self, db):
        """Applied/phone_screen jobs seen 15+ days ago are NOT marked stale."""
        path, conn = db
        _insert_job(conn, "job_applied", "applied", _days_ago(20))
        _insert_job(conn, "job_phone", "phone_screen", _days_ago(20))

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0
        rows = conn.execute("SELECT is_stale FROM jobs").fetchall()
        assert all(r["is_stale"] == 0 for r in rows)

    def test_stale_flag_cleared_when_job_leaves_passive_stage(self, db):
        """A stale discovered job that the user applies to sheds its stale flag."""
        path, conn = db
        _insert_job(conn, "job_now_applied", "applied", _days_ago(20))
        conn.execute("UPDATE jobs SET is_stale = 1 WHERE dedup_key = 'job_now_applied'")
        conn.commit()

        result = run_stale_detection(path)

        assert result["stale_cleared"] == 1
        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'job_now_applied'"
        ).fetchone()
        assert row["is_stale"] == 0

    def test_reseen_job_cleared(self, db):
        """A stale job re-seen recently is cleared (original re-sighting rule)."""
        path, conn = db
        _insert_job(conn, "job_reseen", "discovered", _days_ago(2))
        conn.execute("UPDATE jobs SET is_stale = 1 WHERE dedup_key = 'job_reseen'")
        conn.commit()

        result = run_stale_detection(path)

        assert result["stale_cleared"] == 1
        row = conn.execute("SELECT is_stale FROM jobs WHERE dedup_key = 'job_reseen'").fetchone()
        assert row["is_stale"] == 0


class TestConfigurableThresholds:
    """staleness.stale_threshold_days / archive_threshold_days override defaults."""

    def test_custom_stale_threshold(self, db):
        """stale_threshold_days=5 marks a job seen 7 days ago (default 14 would not)."""
        path, conn = db
        _insert_job(conn, "job_week", "discovered", _days_ago(7))

        config = {"staleness": {"stale_threshold_days": 5}}
        result = run_stale_detection(path, config)

        assert result["stale_marked"] == 1

    def test_custom_archive_threshold(self, db):
        """archive_threshold_days=10 archives a job seen 12 days ago, with
        threshold-accurate evidence."""
        path, conn = db
        _insert_job(conn, "job_old", "discovered", _days_ago(12))

        config = {"staleness": {"archive_threshold_days": 10}}
        result = run_stale_detection(path, config)

        assert result["archived"] == 1
        event = conn.execute(
            "SELECT evidence FROM pipeline_events WHERE job_id = 'job_old'"
        ).fetchone()
        assert event["evidence"] == "not_seen_10_days"

    def test_defaults_without_config(self, db):
        """No config → 14/30 defaults: a 7-day-old sighting is neither stale nor archived."""
        path, conn = db
        _insert_job(conn, "job_fresh", "discovered", _days_ago(7))

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0
        assert result["archived"] == 0


class TestUnverifiedThreshold:
    """expiry_status='inconclusive' jobs use the shorter unverified_stale_threshold_days
    instead of the standard stale_threshold_days (root-cause fix: sources like Jooble
    whose links sit behind a bot-challenge the cascade can never resolve were sitting
    at 'inconclusive' forever and were invisible to the 14-day clock for weeks)."""

    def test_inconclusive_job_stales_before_standard_threshold(self, db):
        """expiry_status='inconclusive', last_seen 6 days ago → stale under the
        5-day unverified default, even though the standard 14-day threshold
        would not have caught it yet."""
        path, conn = db
        _insert_job(
            conn, "job_unverified", "discovered", _days_ago(6), expiry_status="inconclusive"
        )

        result = run_stale_detection(path)

        assert result["stale_marked"] == 1
        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'job_unverified'"
        ).fetchone()
        assert row["is_stale"] == 1

    def test_inconclusive_job_within_unverified_window_not_stale(self, db):
        """expiry_status='inconclusive', last_seen 2 days ago → still within the
        5-day unverified grace period, not yet stale."""
        path, conn = db
        _insert_job(
            conn, "job_recent_unverified", "discovered", _days_ago(2), expiry_status="inconclusive"
        )

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0

    def test_never_checked_job_keeps_standard_threshold(self, db):
        """expiry_status IS NULL (never checked yet), last_seen 6 days ago →
        standard 14-day threshold applies, NOT the shorter unverified one."""
        path, conn = db
        _insert_job(conn, "job_unchecked", "discovered", _days_ago(6), expiry_status=None)

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0

    def test_confirmed_live_job_keeps_standard_threshold(self, db):
        """expiry_status='live', last_seen 6 days ago → standard 14-day
        threshold applies, not the shorter unverified one."""
        path, conn = db
        _insert_job(conn, "job_live", "discovered", _days_ago(6), expiry_status="live")

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0

    def test_custom_unverified_threshold(self, db):
        """unverified_stale_threshold_days=1 marks an inconclusive job seen 2
        days ago, which the 5-day default would not yet catch."""
        path, conn = db
        _insert_job(
            conn, "job_custom_unverified", "discovered", _days_ago(2), expiry_status="inconclusive"
        )

        config = {"staleness": {"unverified_stale_threshold_days": 1}}
        result = run_stale_detection(path, config)

        assert result["stale_marked"] == 1


# ---------- Section 4 archival policy (job-listing-verification Plan 3) ----------


def _insert_company(conn, name, *, company_id, scan_enabled=1, ats_probe_status="pending"):
    conn.execute(
        "INSERT INTO companies (id, name, name_raw, scan_enabled, ats_probe_status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, '2025-01-01', '2025-01-01')",
        (company_id, name, name, scan_enabled, ats_probe_status),
    )


def _insert_unverifiable_job(
    conn,
    dedup_key,
    *,
    first_seen,
    company_id=None,
    careers_checked_at=None,
    direct_url=None,
    direct_url_attempts=0,
    pipeline_status="discovered",
):
    """A job whose sources are entirely within the (test-config) opaque
    registry — sources=['portal_jooble'], matching the _CONFIG fixture below.

    Note: last_seen is set to a recent value to prevent the standard archiver
    from archiving the job before the unverifiable archiver runs (the standard
    archiver uses last_seen, while the unverifiable archiver uses first_seen).
    """
    recent_last_seen = _days_ago(1)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "is_stale, pipeline_status, company_id, sources, source_urls, careers_checked_at, "
        "direct_url, direct_url_attempts) "
        "VALUES (?, 'DS', 'Acme', 'Remote', ?, ?, 0, ?, ?, "
        "'[\"portal_jooble\"]', '[\"https://jooble.org/away/1\"]', ?, ?, ?)",
        (
            dedup_key,
            first_seen,
            recent_last_seen,
            pipeline_status,
            company_id,
            careers_checked_at,
            direct_url,
            direct_url_attempts,
        ),
    )
    conn.commit()


_CONFIG = {
    "verification": {
        "opaque_redirect_sources": [{"source_tag": "portal_jooble", "domain": "jooble.org"}]
    },
    "staleness": {
        "unverified_grace_days": 14,
        "unverifiable_ceiling_days": 60,
        "unverified_stale_threshold_days": 5,
    },
    "direct_link": {"resolver": {"max_attempts": 3}},
}


class TestUnverifiableArchivalBranches:
    def test_branch1_no_company_id_archives_after_grace(self, db):
        db_path, conn = db
        _insert_unverifiable_job(conn, "b1|old", first_seen=_days_ago(20), company_id=None)
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 1
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'b1|old'"
        ).fetchone()
        assert row["pipeline_status"] == "archived"

    def test_branch1_no_company_id_not_yet_within_grace(self, db):
        db_path, conn = db
        _insert_unverifiable_job(conn, "b1|new", first_seen=_days_ago(5), company_id=None)
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_branch2_scan_disabled_archives_after_grace(self, db):
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, scan_enabled=0)
        _insert_unverifiable_job(conn, "b2|old", first_seen=_days_ago(20), company_id=1)
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 1

    def test_branch2_scan_enabled_not_archived(self, db):
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, scan_enabled=1, ats_probe_status="pending")
        _insert_unverifiable_job(conn, "b2|active", first_seen=_days_ago(20), company_id=1)
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_branch3_ats_miss_with_careers_checked_archives(self, db):
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="miss")
        _insert_unverifiable_job(
            conn,
            "b3|checked",
            first_seen=_days_ago(20),
            company_id=1,
            careers_checked_at="2026-06-01T00:00:00",
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 1

    def test_branch3_ats_miss_without_careers_checked_not_archived(self, db):
        """The one reachable signal (careers page) was never actually tried
        — not yet exhausted, so not yet archived under branch 3 (may still
        hit the ceiling backstop eventually, but not within this grace-only
        window)."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="miss")
        _insert_unverifiable_job(
            conn, "b3|unchecked", first_seen=_days_ago(20), company_id=1, careers_checked_at=None
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_branch4_hit_attempts_exhausted_and_careers_checked_archives(self, db):
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="hit")
        _insert_unverifiable_job(
            conn,
            "b4|exhausted",
            first_seen=_days_ago(20),
            company_id=1,
            careers_checked_at="2026-06-01T00:00:00",
            direct_url_attempts=3,
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 1

    def test_branch4_hit_attempts_not_yet_exhausted_not_archived(self, db):
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="hit")
        _insert_unverifiable_job(
            conn,
            "b4|retrying",
            first_seen=_days_ago(20),
            company_id=1,
            careers_checked_at="2026-06-01T00:00:00",
            direct_url_attempts=1,
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_branch4_hit_exhausted_but_careers_never_checked_not_archived(self, db):
        """Both reachable signals must be tried — board-match exhausted but
        careers-page never attempted is not yet a branch-4 match."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="hit")
        _insert_unverifiable_job(
            conn,
            "b4|no_careers",
            first_seen=_days_ago(20),
            company_id=1,
            careers_checked_at=None,
            direct_url_attempts=5,
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_confirmed_evidence_reason_recorded(self, db):
        db_path, conn = db
        _insert_unverifiable_job(conn, "b1|evidence", first_seen=_days_ago(20), company_id=None)
        run_stale_detection(db_path, _CONFIG)
        event = conn.execute(
            "SELECT evidence, source FROM pipeline_events "
            "WHERE job_id = 'b1|evidence' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        assert event["evidence"] == UNVERIFIABLE_EVIDENCE_CONFIRMED
        assert event["source"] == "stale_detector"

    def test_mixed_provenance_never_archived(self, db):
        """A job with even one real (non-opaque) source is never touched by
        this policy, regardless of age or branch state."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="miss")
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "is_stale, pipeline_status, company_id, sources, source_urls, careers_checked_at) "
            "VALUES ('mixed|1', 'DS', 'Acme', 'Remote', ?, ?, 0, 'discovered', 1, "
            '\'["portal_jooble", "greenhouse"]\', '
            '\'["https://jooble.org/away/1", "https://boards.greenhouse.io/acme/jobs/1"]\', '
            "'2026-06-01T00:00:00')",
            (_days_ago(90), _days_ago(1)),
        )
        conn.commit()
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_already_corroborated_job_never_archived(self, db):
        """direct_url is set — this is the exact population Section 3's
        careers-page repair / Plan 1's promotion fix is supposed to save;
        never archived regardless of branch/age."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="miss")
        _insert_unverifiable_job(
            conn,
            "corroborated|1",
            first_seen=_days_ago(90),
            company_id=1,
            direct_url="https://acme.com/jobs/1",
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0


# ---------- Issue #1077: retroactive opaque-source stamp and decay-clock fix ----------
#
# The private repo's tests below drive a REAL code path — the actual
# migration's `.py` helper via a live MigrationContext (_run_m205891047), or
# the actual run_stale_detection() end-to-end. The migration half of that
# (m205891047_retroactive_opaque_inconclusive_stamp) is host-owned and not
# ported to the engine, so TestIssue1077RetroactiveStamp is skipped wholesale
# below, and the two PredicateParity tests that call the migration helper are
# skipped individually. Everything that only drives run_stale_detection()
# ports unchanged.


@pytest.mark.skip(
    reason=(
        "Drives job_finder.web migration m205891047 through a real "
        "MigrationContext (job_finder.web.db_migrate.MIGRATIONS / "
        "job_finder.web.migrations.types.MigrationContext) — migrations are "
        "host-owned and have no equivalent in the engine. See the private "
        "repo's tests/test_stale_detector.py::TestIssue1077RetroactiveStamp "
        "for the original migration-level assertions; nothing here tests "
        "stale_detector.py itself (it tests the migration)."
    )
)
class TestIssue1077RetroactiveStamp:
    """Not ported — see class skip reason. Original class exercised
    migration m205891047's `.py` helper directly against a real
    MigrationContext; every test needs the private repo's MIGRATIONS
    registry, which does not exist in the engine."""

    def test_null_status_gated_only_row_stamped_inconclusive(self, db):
        pass

    def test_jooble_linkedin_row_untouched(self, db):
        pass

    def test_live_gated_row_untouched(self, db):
        pass

    def test_cohort_coverage_outside_passive_statuses(self, db):
        pass

    def test_archived_row_untouched(self, db):
        pass

    def test_idempotent_second_run_is_noop(self, db):
        pass


class TestIssue1077DecayClockFix:
    """End-to-end tests of the decay-clock fix via run_stale_detection() —
    not just the shared predicate in isolation. These fail on current main:
    the mark pass correctly sets is_stale=1 for the gated-only row, but the
    pre-fix clear pass (in the same transaction, same call) immediately
    resets it to 0 because the row's last_seen looks fresh."""

    def test_gated_only_inconclusive_fresh_last_seen_ends_stale(self, db):
        """Issue's named regression fixture: gated-only inconclusive row,
        fresh last_seen (a Jooble re-sighting), expiry_checked_at 6 days old
        (past the 5-day unverified threshold) -> final is_stale=1.

        On current main (pre-fix): stale_marked=1 (the gated-only mark pass
        fires correctly) but stale_cleared=1 too (the naive last_seen-based
        clear arm immediately undoes it) -> final is_stale=0. This is the
        exact 1,612-row infinite loop the issue reports.
        """
        db_path, conn = db
        _insert_job(
            conn,
            "gated_regression",
            "discovered",
            _days_ago(1),  # fresh last_seen (re-sighted yesterday)
            expiry_status="inconclusive",
            sources='["portal_jooble"]',
            source_urls='["https://jooble.org/away/1"]',
            expiry_checked_at=_days_ago(6),  # past the 5-day unverified threshold
        )

        result = run_stale_detection(db_path, _CONFIG)

        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'gated_regression'"
        ).fetchone()
        assert row["is_stale"] == 1
        assert result["stale_marked"] == 1
        assert result["stale_cleared"] == 0

    def test_non_gated_re_sighting_rescues_the_row(self, db):
        """Same shape, but the job now ALSO carries a real (non-gated)
        source (e.g. corroborated by LinkedIn since the last check). Mixed
        provenance is never gated-only, so the row is rescued via the
        ordinary last_seen-based clear arm -- even though it starts already
        flagged stale from an earlier run."""
        db_path, conn = db
        _insert_job(
            conn,
            "mixed_rescued",
            "discovered",
            _days_ago(1),
            expiry_status="inconclusive",
            sources='["portal_jooble", "linkedin"]',
            source_urls='["https://jooble.org/away/1", "https://linkedin.com/jobs/1"]',
            expiry_checked_at=_days_ago(6),
            is_stale=1,
        )

        result = run_stale_detection(db_path, _CONFIG)

        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'mixed_rescued'"
        ).fetchone()
        assert row["is_stale"] == 0
        assert result["stale_cleared"] == 1

    def test_corroborated_source_job_with_fresh_last_seen_not_marked_stale(self, db):
        """A mixed-provenance job (not gated-only) with fresh last_seen is
        never marked stale in the first place — the two-tier mark pass
        applies the standard last_seen check, which a 1-day-old sighting
        does not fail."""
        db_path, conn = db
        _insert_job(
            conn,
            "mixed_fresh",
            "discovered",
            _days_ago(1),
            expiry_status="inconclusive",
            sources='["portal_jooble", "linkedin"]',
            source_urls='["https://jooble.org/away/1", "https://linkedin.com/jobs/1"]',
            expiry_checked_at=_days_ago(6),
        )

        result = run_stale_detection(db_path, _CONFIG)

        row = conn.execute("SELECT is_stale FROM jobs WHERE dedup_key = 'mixed_fresh'").fetchone()
        assert row["is_stale"] == 0
        assert result["stale_marked"] == 0


class TestIssue1077PredicateParity:
    """The migration's candidate cohort and the stale detector's gated-only
    cohort must agree with is_opaque_redirect_source on every row in a
    shared fixture -- one predicate, not two independently-maintained
    copies -- verified both individually and as a composed pipeline."""

    # (dedup_key, sources, source_urls, expected_gated_only)
    _FIXTURE_ROWS = [
        ("gated_only", '["portal_jooble"]', '["https://jooble.org/away/1"]', True),
        (
            "mixed",
            '["portal_jooble", "linkedin"]',
            '["https://jooble.org/away/1", "https://linkedin.com/jobs/1"]',
            False,
        ),
        ("empty_sources", "[]", "[]", False),
    ]

    @pytest.mark.skip(
        reason=(
            "Drives migration m205891047 via _run_m205891047 as setup — "
            "not ported (see module docstring); migrations are host-owned."
        )
    )
    def test_migration_cohort_matches_predicate(self, db):
        pass

    def test_decay_clock_cohort_matches_predicate(self, db):
        """Every inconclusive row (fresh last_seen, old expiry_checked_at)
        the decay-clock's gated-only mark pass stales is exactly the set
        is_opaque_redirect_source flags True on the shared fixture."""
        db_path, conn = db
        for dedup_key, sources, source_urls, _ in self._FIXTURE_ROWS:
            _insert_job(
                conn,
                dedup_key,
                "discovered",
                _days_ago(1),
                expiry_status="inconclusive",
                sources=sources,
                source_urls=source_urls,
                expiry_checked_at=_days_ago(6),
            )

        run_stale_detection(db_path, _CONFIG)

        for dedup_key, _, _, expected_gated in self._FIXTURE_ROWS:
            row = conn.execute(
                "SELECT is_stale FROM jobs WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            assert bool(row["is_stale"]) == expected_gated, dedup_key

    @pytest.mark.skip(
        reason=(
            "Drives migration m205891047 via _run_m205891047 as its first "
            "phase — not ported (see module docstring); migrations are "
            "host-owned."
        )
    )
    def test_full_pipeline_migration_then_decay(self, db):
        pass


class TestUnverifiableHardCeilingBackstop:
    def test_ceiling_fires_with_no_branch_match(self, db):
        """ats_probe_status stuck at 'pending' forever matches none of the
        four branches — only the ceiling can ever archive this population."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="pending")
        _insert_unverifiable_job(conn, "ceiling|1", first_seen=_days_ago(65), company_id=1)
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 1
        event = conn.execute(
            "SELECT evidence FROM pipeline_events "
            "WHERE job_id = 'ceiling|1' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        assert event["evidence"] == UNVERIFIABLE_EVIDENCE_CEILING

    def test_ceiling_does_not_fire_before_it_elapses(self, db):
        """Past the grace period but not the ceiling, and no branch matches
        — neither archive path fires yet."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="pending")
        _insert_unverifiable_job(conn, "notyet|1", first_seen=_days_ago(30), company_id=1)
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 0

    def test_ceiling_supersedes_branch_reason_when_both_would_apply(self, db):
        """A job old enough for the ceiling AND matching a branch gets the
        ceiling's evidence tag, not the branch's — ceiling fires
        unconditionally, per spec, once past that threshold."""
        db_path, conn = db
        _insert_company(conn, "Acme", company_id=1, ats_probe_status="miss")
        _insert_unverifiable_job(
            conn,
            "both|1",
            first_seen=_days_ago(65),
            company_id=1,
            careers_checked_at="2026-06-01T00:00:00",
        )
        result = run_stale_detection(db_path, _CONFIG)
        assert result["unverifiable_archived"] == 1
        event = conn.execute(
            "SELECT evidence FROM pipeline_events "
            "WHERE job_id = 'both|1' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        assert event["evidence"] == UNVERIFIABLE_EVIDENCE_CEILING
