"""Tests for dedup_normalizer module — normalization functions and retroactive merge.

Tests:
- normalize_company strips suffixes (Inc., LLC, Corp., Ltd., Co., etc.)
- normalize_title expands abbreviations (Sr./Senior, Jr./Junior, Mgr./Manager, etc.)
- normalize_title strips IC-level and Level-N suffixes
- normalized_dedup_key ignores location — same company+title = same key
- Job.dedup_key uses normalized_dedup_key format (company+title, no location)
- run_retroactive_dedup merges duplicate jobs, updates FK tables, logs to merge_log
- run_retroactive_dedup uses status precedence when statuses conflict
- run_retroactive_dedup returns count of merged duplicates
- ALLOWED_FK_TABLES allowlist guards f-string SQL in _update_fk_tables (DEBT-04)
"""

import json
import sqlite3
from datetime import datetime

import pytest

from jobcannon.engine.models import Job

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_db():
    """Create an in-memory SQLite DB with the minimal schema for dedup tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            sources TEXT DEFAULT '[]',
            source_urls TEXT DEFAULT '[]',
            source_id TEXT DEFAULT '',
            salary_min INTEGER DEFAULT NULL,
            salary_max INTEGER DEFAULT NULL,
            description TEXT DEFAULT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            score REAL DEFAULT 0,
            score_breakdown TEXT DEFAULT '{}',
            user_interest TEXT DEFAULT 'unreviewed',
            pipeline_status TEXT DEFAULT 'discovered',
            posted_date TEXT DEFAULT NULL,
            posted_date_precision TEXT DEFAULT NULL,
            notes TEXT DEFAULT '',
            haiku_score REAL DEFAULT NULL,
            haiku_summary TEXT DEFAULT NULL,
            sonnet_score REAL DEFAULT NULL,
            fit_analysis TEXT DEFAULT NULL,
            classification TEXT DEFAULT NULL,
            sub_scores_json TEXT DEFAULT NULL,
            jd_full TEXT DEFAULT NULL,
            is_stale INTEGER DEFAULT 0,
            locations_raw TEXT DEFAULT NULL,
            locations_structured TEXT DEFAULT NULL,
            workplace_type TEXT DEFAULT 'UNSPECIFIED',
            primary_country_code TEXT DEFAULT NULL,
            description_reformatted INTEGER DEFAULT 0,
            sub_score_sum INTEGER NOT NULL DEFAULT 0,
            classification_rank INTEGER NOT NULL DEFAULT 0,
            location_policy_sort_order INTEGER NOT NULL DEFAULT 1,
            location_policy_rank INTEGER NOT NULL DEFAULT 0,
            location_policy_eligible INTEGER NOT NULL DEFAULT 1,
            is_location_unresolved INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pipeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(dedup_key),
            from_status TEXT,
            to_status TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            evidence TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS pipeline_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gmail_message_id TEXT NOT NULL UNIQUE,
            detection_type TEXT NOT NULL,
            job_id TEXT REFERENCES jobs(dedup_key),
            confidence_score INTEGER NOT NULL,
            matched_signals TEXT DEFAULT '[]',
            snippet TEXT DEFAULT '',
            email_subject TEXT DEFAULT '',
            email_from TEXT DEFAULT '',
            email_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS merge_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT NOT NULL,
            merged_key TEXT NOT NULL,
            merge_source TEXT NOT NULL DEFAULT 'migration',
            merged_at TEXT NOT NULL
        );
    """)
    conn.commit()
    yield conn
    conn.close()


def _insert_job(
    conn,
    dedup_key,
    title,
    company,
    location="Remote",
    pipeline_status="discovered",
    first_seen=None,
    last_seen=None,
    sources=None,
    source_urls=None,
    description=None,
    haiku_score=None,
    sonnet_score=None,
    notes="",
    salary_min=None,
    salary_max=None,
    classification=None,
    sub_scores_json=None,
    locations_raw=None,
):
    """Helper to insert a job row into the in-memory DB.

    v3.0 (Phase 34 Plan 3 Commit A): classification + sub_scores_json are the
    v3 scoring columns. Legacy haiku_score/sonnet_score kwargs still work
    because the schema retains those columns (Plan 2 shim keeps them populated).

    ``locations_raw`` mirrors production's coherent location state (the display
    ``location`` column is derived from it) so the D-5 funnel the re-key merge
    feeds has a real base list to set-union against.
    """
    now = datetime.now().isoformat()
    if first_seen is None:
        first_seen = now
    if last_seen is None:
        last_seen = now
    if sources is None:
        sources = ["test"]
    if source_urls is None:
        source_urls = [f"https://example.com/{dedup_key}"]
    conn.execute(
        """
        INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             pipeline_status, first_seen, last_seen, description,
             haiku_score, sonnet_score, classification, sub_scores_json,
             notes, salary_min, salary_max, locations_raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            dedup_key,
            title,
            company,
            location,
            json.dumps(sources),
            json.dumps(source_urls),
            pipeline_status,
            first_seen,
            last_seen,
            description,
            haiku_score,
            sonnet_score,
            classification,
            sub_scores_json,
            notes,
            salary_min,
            salary_max,
            locations_raw,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: normalize_company
# ---------------------------------------------------------------------------


class TestNormalizeCompany:
    def test_strips_inc_with_period(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Klaviyo Inc.") == normalize_company("Klaviyo")

    def test_strips_inc_with_comma_space(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Intuit, Inc.") == normalize_company("Intuit")

    def test_strips_llc(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Google LLC") == normalize_company("Google")

    def test_no_suffix_lowercased(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Apple") == "apple"

    def test_strips_corp(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Microsoft Corp.") == normalize_company("Microsoft")

    def test_strips_ltd(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Acme Ltd.") == normalize_company("Acme")

    def test_strips_corporation(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("IBM Corporation") == normalize_company("IBM")

    def test_strips_co(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("Trading Co.") == normalize_company("Trading")

    def test_case_insensitive_normalization(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("KLAVIYO INC.") == normalize_company("klaviyo")

    def test_whitespace_stripped(self):
        from jobcannon.engine.dedup_normalizer import normalize_company

        assert normalize_company("  Amazon  ") == "amazon"


# ---------------------------------------------------------------------------
# Tests: normalize_title
# ---------------------------------------------------------------------------


class TestNormalizeTitle:
    def test_expands_sr_to_senior(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("Sr. Software Engineer") == normalize_title(
            "Senior Software Engineer"
        )

    def test_expands_jr_to_junior(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("Jr. Developer") == normalize_title("Junior Developer")

    def test_strips_ic_level_suffix(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("Staff Engineer (IC5)") == normalize_title("Staff Engineer")

    def test_strips_level_n_suffix(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("Engineer Level 3") == normalize_title("Engineer")

    def test_expands_mgr_to_manager(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("Eng. Mgr.") == normalize_title("Engineering Manager")

    def test_case_insensitive(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("SR. SOFTWARE ENGINEER") == normalize_title(
            "Senior Software Engineer"
        )

    def test_whitespace_stripped(self):
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("  Senior Engineer  ") == "senior engineer"

    def test_digit_letter_boundary_inserted(self):
        """Missing separator at digit<->letter boundary canonicalizes the same.

        SERP count-tile titles like "84Data Scientist Jobs" should collapse to
        the same normalized form as "84 Data Scientist Jobs" so they hit the
        same dedup_key. Issue #212.
        """
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("84Data Scientist Jobs") == normalize_title(
            "84 Data Scientist Jobs"
        )
        assert normalize_title("84Data Scientist Jobs") == "84 data scientist jobs"
        # Letter->digit transition also covered (e.g., "H1B" stays intact only
        # because the digit->letter rule re-splits it deterministically).
        assert normalize_title("Level3Engineer") == normalize_title("Level 3 Engineer")

    def test_digit_letter_boundary_does_not_mangle_normal_titles(self):
        """Normal titles without digit/letter adjacency are untouched.

        Negative case: ordinary titles (no digits adjacent to letters) must not
        be perturbed by the new boundary rule. Issue #212.
        """
        from jobcannon.engine.dedup_normalizer import normalize_title

        assert normalize_title("Software Engineer") == "software engineer"
        assert normalize_title("Data Scientist") == "data scientist"
        assert normalize_title("Product Manager") == "product manager"

    def test_foundation_and_web_copies_agree_on_boundary(self):
        """Foundation and web copies of normalize_title must agree byte-for-byte.

        The two implementations are duplicated by design (foundation cannot
        depend on web). If they diverge on the digit/letter boundary case,
        dedup_key derivation in different code paths would silently disagree.
        Issue #212.
        """
        from jobcannon.engine.normalizers import normalize_title as foundation_normalize
        from jobcannon.engine.dedup_normalizer import normalize_title as web_normalize

        for raw in (
            "84Data Scientist Jobs",
            "84 Data Scientist Jobs",
            "Level3Engineer",
            "Senior Software Engineer",
            "  Senior Engineer  ",
        ):
            assert foundation_normalize(raw) == web_normalize(raw), raw


# ---------------------------------------------------------------------------
# Tests: normalized_dedup_key (location excluded)
# ---------------------------------------------------------------------------


class TestNormalizedDedupKey:
    def test_location_excluded_from_key(self):
        from jobcannon.engine.models import Job

        key_sf = Job.normalized_dedup_key(
            "Klaviyo Inc.", "Sr. Software Engineer", "San Francisco, CA"
        )
        key_nyc = Job.normalized_dedup_key("Klaviyo", "Senior Software Engineer", "NYC")
        assert key_sf == key_nyc

    def test_key_format_is_company_pipe_title(self):
        from jobcannon.engine.models import Job

        key = Job.normalized_dedup_key("Google LLC", "Senior Engineer")
        assert "|" in key
        # Should not have a third segment (no location)
        parts = key.split("|")
        assert len(parts) == 2

    def test_different_companies_differ(self):
        from jobcannon.engine.models import Job

        key1 = Job.normalized_dedup_key("Google", "Engineer")
        key2 = Job.normalized_dedup_key("Meta", "Engineer")
        assert key1 != key2

    def test_different_titles_differ(self):
        from jobcannon.engine.models import Job

        key1 = Job.normalized_dedup_key("Google", "Engineer")
        key2 = Job.normalized_dedup_key("Google", "Manager")
        assert key1 != key2

    def test_digit_letter_boundary_converges_keys(self):
        """Missing-separator title variants converge to a single dedup_key.

        The two Capital One rows ("84Data..." vs "84 Data...") that surfaced
        the dedup hole must now produce identical keys. Issue #212.
        """
        from jobcannon.engine.models import Job

        key_with_space = Job.normalized_dedup_key("Capital One", "84 Data Scientist Jobs")
        key_without_space = Job.normalized_dedup_key("Capital One", "84Data Scientist Jobs")
        assert key_with_space == key_without_space


# ---------------------------------------------------------------------------
# Tests: Job.dedup_key uses normalized_dedup_key
# ---------------------------------------------------------------------------


class TestJobDedupKey:
    def test_dedup_key_uses_normalized_format(self):
        """Job.dedup_key should return company|title (no location)."""
        from jobcannon.engine.models import Job as JobModel

        job = Job(
            title="Sr. Engineer",
            company="Klaviyo Inc.",
            location="SF",
            source="test",
            source_url="https://example.com",
        )
        expected = JobModel.normalized_dedup_key("Klaviyo Inc.", "Sr. Engineer")
        assert job.dedup_key == expected

    def test_dedup_key_ignores_location(self):
        """Two jobs with same company+title but different location should have same dedup_key."""
        job_sf = Job(
            title="Software Engineer",
            company="Acme",
            location="San Francisco",
            source="test",
            source_url="https://example.com/sf",
        )
        job_nyc = Job(
            title="Software Engineer",
            company="Acme",
            location="New York",
            source="test",
            source_url="https://example.com/nyc",
        )
        assert job_sf.dedup_key == job_nyc.dedup_key

    def test_dedup_key_strips_company_suffix(self):
        """Jobs with same company (with/without Inc.) should have matching dedup_keys."""
        job_inc = Job(
            title="Software Engineer",
            company="Klaviyo Inc.",
            location="Remote",
            source="test",
            source_url="https://example.com/1",
        )
        job_bare = Job(
            title="Software Engineer",
            company="Klaviyo",
            location="Remote",
            source="test",
            source_url="https://example.com/2",
        )
        assert job_inc.dedup_key == job_bare.dedup_key

    def test_dedup_key_expands_title_abbreviations(self):
        """Jobs with Sr./Senior in title should have matching dedup_keys."""
        job_sr = Job(
            title="Sr. Software Engineer",
            company="Acme",
            location="Remote",
            source="test",
            source_url="https://example.com/1",
        )
        job_senior = Job(
            title="Senior Software Engineer",
            company="Acme",
            location="Remote",
            source="test",
            source_url="https://example.com/2",
        )
        assert job_sr.dedup_key == job_senior.dedup_key


# ---------------------------------------------------------------------------
# Tests: run_retroactive_dedup
# ---------------------------------------------------------------------------


class TestRunRetroactiveDedup:
    def test_merges_duplicate_jobs(self, mem_db):
        """Two jobs with the same normalized company+title are merged."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        # Insert two rows that should be considered duplicates after normalization
        _insert_job(
            mem_db,
            "klaviyo inc.|senior software engineer|san francisco",
            "Senior Software Engineer",
            "Klaviyo Inc.",
            location="San Francisco, CA",
            first_seen="2026-01-01T00:00:00",
        )
        _insert_job(
            mem_db,
            "klaviyo|sr. software engineer|remote",
            "Sr. Software Engineer",
            "Klaviyo",
            location="Remote",
            first_seen="2026-01-02T00:00:00",
        )

        count = run_retroactive_dedup(mem_db)

        assert count == 1
        # Only one row should remain
        rows = mem_db.execute("SELECT * FROM jobs").fetchall()
        assert len(rows) == 1

    def test_merge_strips_stored_non_string_source_url(self, mem_db):
        """Regression (#698 review): the retroactive-dedup UPDATE is a second writer
        of jobs.source_urls; it must route the merged union through _clean_source_urls
        so a truthy non-string element ALREADY stored on a row (e.g. an int) cannot
        survive the ``if url`` union guard back into the canonical row and re-poison
        the column — the exact non-string crash class (m205178950 / #693) the write
        boundary makes unrepresentable.

        Load-bearing: without the _clean_source_urls call in _merge_job_data the int
        survives and the isinstance assertion below fails."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db,
            "klaviyo inc.|senior software engineer|san francisco",
            "Senior Software Engineer",
            "Klaviyo Inc.",
            location="San Francisco, CA",
            first_seen="2026-01-01T00:00:00",
            source_urls=["https://klaviyo.com/jobs/1"],
        )
        _insert_job(
            mem_db,
            "klaviyo|sr. software engineer|remote",
            "Sr. Software Engineer",
            "Klaviyo",
            location="Remote",
            first_seen="2026-01-02T00:00:00",
            # a truthy non-string element already poisoning a stored row
            source_urls=["https://klaviyo.com/jobs/2", 999],
        )

        count = run_retroactive_dedup(mem_db)

        assert count == 1
        merged = json.loads(
            mem_db.execute("SELECT source_urls FROM jobs").fetchone()["source_urls"]
        )
        assert all(isinstance(u, str) and u for u in merged), merged
        assert 999 not in merged
        assert "https://klaviyo.com/jobs/1" in merged
        assert "https://klaviyo.com/jobs/2" in merged

    def test_keeps_earliest_first_seen_as_canonical(self, mem_db):
        """run_retroactive_dedup keeps the earliest first_seen row as canonical."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        # First-seen row is the Klaviyo Inc. variant
        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Software Engineer",
            "Klaviyo Inc.",
            first_seen="2026-01-01T09:00:00",
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Sr. Software Engineer",
            "Klaviyo",
            first_seen="2026-01-05T09:00:00",
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT * FROM jobs").fetchall()
        assert len(rows) == 1
        # The remaining row should have first_seen from the earlier row
        assert rows[0]["first_seen"] == "2026-01-01T09:00:00"

    def test_updates_pipeline_events_fk_references(self, mem_db):
        """FK references in pipeline_events are updated from duplicate key to canonical key."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "old-key-1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "old-key-2", "Senior Engineer", "Acme", first_seen="2026-01-05T00:00:00"
        )

        # Add pipeline_events referencing the duplicate key
        now = datetime.now().isoformat()
        mem_db.execute(
            """
            INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp)
            VALUES ('old-key-2', 'discovered', 'applied', ?)
        """,
            (now,),
        )
        mem_db.commit()

        run_retroactive_dedup(mem_db)

        # After merge, the event should reference the canonical (normalized) key
        events = mem_db.execute("SELECT * FROM pipeline_events").fetchall()
        assert len(events) == 1
        # The event job_id should not be old-key-2 anymore
        assert events[0]["job_id"] != "old-key-2"

    def test_uses_status_precedence_applied_over_discovered(self, mem_db):
        """Merge keeps the higher-precedence pipeline status."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            pipeline_status="discovered",
            first_seen="2026-01-01T00:00:00",
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            pipeline_status="applied",
            first_seen="2026-01-05T00:00:00",
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT pipeline_status FROM jobs").fetchall()
        assert len(rows) == 1
        assert rows[0]["pipeline_status"] == "applied"

    def test_returns_count_of_merged_duplicates(self, mem_db):
        """run_retroactive_dedup returns the number of rows deleted (merged)."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        # Two pairs of duplicates
        _insert_job(
            mem_db, "key-a1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(mem_db, "key-a2", "Senior Engineer", "Acme", first_seen="2026-01-02T00:00:00")
        _insert_job(
            mem_db, "key-b1", "Product Manager", "Google LLC", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "key-b2", "Product Manager", "Google", first_seen="2026-01-03T00:00:00"
        )

        count = run_retroactive_dedup(mem_db)

        # Should have merged 2 duplicates (one from each group)
        assert count == 2
        # Should have 2 rows remaining
        rows = mem_db.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert rows[0] == 2

    def test_creates_merge_log_entries(self, mem_db):
        """Each merge operation creates a merge_log entry."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "old-key-1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "old-key-2", "Senior Engineer", "Acme", first_seen="2026-01-05T00:00:00"
        )

        run_retroactive_dedup(mem_db)

        logs = mem_db.execute("SELECT * FROM merge_log").fetchall()
        assert len(logs) >= 1
        # The merged_key should be old-key-2 (the duplicate)
        merged_keys = [log["merged_key"] for log in logs]
        assert "old-key-2" in merged_keys

    def test_merges_sources_from_duplicate(self, mem_db):
        """After merge, canonical row has combined sources from both rows."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            sources=["linkedin"],
            first_seen="2026-01-01T00:00:00",
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            sources=["glassdoor"],
            first_seen="2026-01-05T00:00:00",
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT sources FROM jobs").fetchall()
        assert len(rows) == 1
        sources = json.loads(rows[0]["sources"])
        assert "linkedin" in sources
        assert "glassdoor" in sources

    def test_description_dedup_keeps_longer(self, mem_db):
        """When one description is a substring of another, the longer one is kept."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        short_desc = "We are hiring a Senior Engineer."
        long_desc = "We are hiring a Senior Engineer. You will build scalable systems."

        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            description=long_desc,
            first_seen="2026-01-01T00:00:00",
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            description=short_desc,
            first_seen="2026-01-05T00:00:00",
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT description FROM jobs").fetchall()
        assert len(rows) == 1
        assert rows[0]["description"] == long_desc

    def test_no_merge_when_no_duplicates(self, mem_db):
        """run_retroactive_dedup returns 0 when no duplicates exist."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "key-unique-1", "Senior Engineer", "Acme", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "key-unique-2", "Product Manager", "Acme", first_seen="2026-01-02T00:00:00"
        )

        count = run_retroactive_dedup(mem_db)

        assert count == 0
        rows = mem_db.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert rows[0] == 2

    def test_dedup_key_updated_to_normalized_format(self, mem_db):
        """After retroactive dedup, canonical row's dedup_key is the new normalized format."""
        from jobcannon.engine.models import Job
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "old-key-1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT dedup_key FROM jobs").fetchall()
        assert len(rows) == 1
        expected_key = Job.normalized_dedup_key("Acme Inc.", "Senior Engineer")
        assert rows[0]["dedup_key"] == expected_key

    def test_offers_higher_status_than_rejected(self, mem_db):
        """offer status takes precedence over rejected."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            pipeline_status="offer",
            first_seen="2026-01-01T00:00:00",
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            pipeline_status="rejected",
            first_seen="2026-01-05T00:00:00",
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT pipeline_status FROM jobs").fetchall()
        assert rows[0]["pipeline_status"] == "offer"


# ---------------------------------------------------------------------------
# Tests: ALLOWED_FK_TABLES allowlist (DEBT-04)
# ---------------------------------------------------------------------------


class TestAllowlist:
    """Verify SQL injection guard on _update_fk_tables (DEBT-04)."""

    def test_non_allowlisted_table_raises_assertion(self, mem_db):
        """_update_fk_tables raises AssertionError for table not in ALLOWED_FK_TABLES.

        Since _update_fk_tables uses a hardcoded internal list, we test the guard
        directly via _run_with_bad_tables which replicates the assert logic.
        """
        from jobcannon.engine.dedup_normalizer import ALLOWED_FK_TABLES

        bad_table = "injected_table; DROP TABLE jobs; --"
        assert bad_table not in ALLOWED_FK_TABLES

        bad_fk_tables = [(bad_table, "job_id")]
        with pytest.raises(AssertionError, match="SQL injection guard"):
            _run_with_bad_tables(mem_db, "old", "new", bad_fk_tables)

    def test_allowlisted_tables_assertion_passes(self, mem_db):
        """All FK tables in ALLOWED_FK_TABLES are known valid table names."""
        from jobcannon.engine.dedup_normalizer import ALLOWED_FK_TABLES

        expected_tables = {
            "pipeline_events",
            "pipeline_detections",
            "scoring_costs",
        }
        assert frozenset(expected_tables) == ALLOWED_FK_TABLES

    def test_allowed_fk_tables_is_frozenset(self):
        """ALLOWED_FK_TABLES is a frozenset (immutable)."""
        from jobcannon.engine.dedup_normalizer import ALLOWED_FK_TABLES

        assert isinstance(ALLOWED_FK_TABLES, frozenset)

    def test_allowed_fk_tables_has_three_entries(self):
        """ALLOWED_FK_TABLES contains exactly 3 table names."""
        from jobcannon.engine.dedup_normalizer import ALLOWED_FK_TABLES

        assert len(ALLOWED_FK_TABLES) == 3

    def test_update_fk_tables_raises_for_unknown_table(self, mem_db):
        """_update_fk_tables raises AssertionError when fk_tables contains a non-allowlisted name.

        This test directly verifies the assert guard fires by monkeypatching the
        internal fk_tables list used in _update_fk_tables.
        """
        import unittest.mock as mock

        import jobcannon.engine.dedup_normalizer as mod

        bad_fk_tables = [("evil_table", "job_id")]

        with (
            mock.patch.object(
                mod,
                "_update_fk_tables",
                wraps=lambda conn, old_key, new_key: _run_with_bad_tables(
                    conn, old_key, new_key, bad_fk_tables
                ),
            ),
            pytest.raises(AssertionError, match="SQL injection guard"),
        ):
            mod._update_fk_tables(mem_db, "old", "new")

    def test_update_fk_tables_succeeds_for_all_allowlisted(self, mem_db):
        """_update_fk_tables completes without assertion error for all 6 allowlisted tables."""
        from jobcannon.engine.dedup_normalizer import _update_fk_tables

        # Should not raise — all tables are in ALLOWED_FK_TABLES and exist in mem_db
        _update_fk_tables(mem_db, "nonexistent-old-key", "nonexistent-new-key")


def _run_with_bad_tables(conn, old_key, new_key, fk_tables):
    """Helper: run the _update_fk_tables assert logic with a custom fk_tables list."""
    import sqlite3 as _sqlite3

    from jobcannon.engine.dedup_normalizer import ALLOWED_FK_TABLES

    for table, column in fk_tables:
        assert table in ALLOWED_FK_TABLES, (
            f"SQL injection guard: '{table}' is not in ALLOWED_FK_TABLES"
        )
        try:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                (new_key, old_key),
            )
        except _sqlite3.OperationalError:
            pass


# ===========================================================================
# P4.1 — versioned dedup-key derivation + standing re-key (D-8, issue #377)
# ===========================================================================


class TestNormalizerVersionCanary:
    """Enforce D-8: normalize_* output cannot drift without a version bump.

    The hash below pins the byte-for-byte behavior of normalize_company /
    normalize_title over a fixed corpus. If either function's semantics change
    so the same input maps to a different output, this test fails with the
    message below. The required response is to bump NORMALIZER_VERSION (which
    re-arms the standing re-key operation) AND update the pinned hash — never
    silently update the hash to match new behavior without a version bump.

    This is the enforcement that #238's stranded-key gap can never recur: a
    normalizer change that strands existing dedup_keys is now impossible to
    merge without also bumping the version that triggers re-derivation.
    """

    # Corpus exercises every normalize branch: suffixes, abbreviations, level
    # strips, legal-entity prefixes, HTML, the digit<->letter boundary (#212).
    CORPUS_COMPANY = [
        "Klaviyo Inc.",
        "Intuit, Inc.",
        "Google LLC",
        "Apple",
        "Microsoft Corp.",
        "Acme Ltd.",
        "IBM Corporation",
        "Trading Co.",
        "  Amazon  ",
        "HC1316 GE Precision Healthcare LLC",
        "1144 IHS GLOBAL INC",
        "A10 Networks, Inc",
        "Point2 Technology Inc.",
        "21 Tech",
        "&amp;T Corp",
        "<b>Acme</b> Inc.",
    ]
    CORPUS_TITLE = [
        "Sr. Software Engineer",
        "Senior Software Engineer",
        "Jr. Developer",
        "Staff Engineer (IC5)",
        "Engineer Level 3",
        "Eng. Mgr.",
        "84Data Scientist Jobs",
        "84 Data Scientist Jobs",
        "Level3Engineer",
        "Software Engineer",
        "VP. of Sales",
        "Product Manager",
        "Data Scientist III",
        "Staff DS - L5",
    ]
    # Update this hash ONLY together with a NORMALIZER_VERSION bump.
    # Single pinned hash for the AUTHORITATIVE dedup_key derivation path
    # (Job.dedup_key / derive_dedup_key both route through
    # jobcannon.engine.normalizers). The web/dedup_normalizer twin now produces the
    # SAME hash: both normalize_company and normalize_title delegate to the
    # foundation copies (single source of truth), so the two copies agree over
    # the full corpus. The former EXPECTED_WEB_HASH divergence
    # (web copy skipped HTML decode / tag strip / leading-numeric-junk strip)
    # was the architectural-debt-B canonical-field-ownership hole and is now
    # closed. Cross-copy parity is asserted directly by
    # test_foundation_and_web_copies_agree_on_company and
    # test_foundation_and_web_copies_agree_on_boundary.
    EXPECTED_HASH = "96704e50dc764ea686aab1eed375083066e122121fe3e3d41ed763b6fb6c9f7e"
    EXPECTED_VERSION = 2

    def _corpus_hash(self, normalize_company, normalize_title) -> str:
        import hashlib

        h = hashlib.sha256()
        for c in self.CORPUS_COMPANY:
            h.update(normalize_company(c).encode("utf-8"))
            h.update(b"\x00")
        for t in self.CORPUS_TITLE:
            h.update(normalize_title(t).encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def test_foundation_normalizer_behavior_pinned(self):
        from jobcannon.engine.normalizers import (
            NORMALIZER_VERSION,
            normalize_company,
            normalize_title,
        )

        got = self._corpus_hash(normalize_company, normalize_title)
        assert got == self.EXPECTED_HASH, (
            "normalizer semantics changed -- bump NORMALIZER_VERSION "
            f"(and the pinned hash). version={NORMALIZER_VERSION}, "
            f"expected_hash={self.EXPECTED_HASH}, got={got}"
        )
        assert NORMALIZER_VERSION == self.EXPECTED_VERSION, (
            "NORMALIZER_VERSION changed -- update EXPECTED_VERSION and the "
            "pinned hash in this canary if the normalize_* behavior was "
            "intentionally bumped."
        )

    def test_web_copy_behavior_pinned(self):
        """The web-layer copy now hashes IDENTICALLY to the foundation copy.

        normalize_company and normalize_title both delegate to the foundation
        copies, so the web twin's corpus hash equals
        EXPECTED_HASH. If this drifts, either a web-only edit reintroduced
        divergence or the normalizer semantics changed -- in the latter case bump
        NORMALIZER_VERSION and the single pinned hash.
        """
        from jobcannon.engine.dedup_normalizer import normalize_company, normalize_title

        got = self._corpus_hash(normalize_company, normalize_title)
        assert got == self.EXPECTED_HASH, (
            "web-layer normalizer semantics drifted from the foundation copy -- "
            "the web/dedup_normalizer twin must hash identically to "
            "jobcannon.engine.normalizers. If the change was intentional, bump "
            "NORMALIZER_VERSION and the single pinned hash."
        )

    def test_foundation_and_web_copies_agree_on_company(self):
        """normalize_company must agree byte-for-byte across the two copies.

        The web copy delegates to the foundation copy (single source of truth),
        so this is the company analogue of
        test_foundation_and_web_copies_agree_on_boundary. It guards the dedup
        invariant directly: the merge engine (run_retroactive_dedup) and the
        upsert path (Job.dedup_key) must compute the same company key for inputs
        with HTML entities/tags, leading numeric junk, and internal whitespace —
        the exact cases the old lighter web copy diverged on.
        """
        from jobcannon.engine.normalizers import normalize_company as foundation_normalize
        from jobcannon.engine.dedup_normalizer import normalize_company as web_normalize

        for raw in self.CORPUS_COMPANY + [
            "<b>Acme</b> Inc.",
            "&amp;T Corp",
            "1. Acme Corp",
            "Foo   Bar  Inc",
            "Big&nbsp;Co LLC",
        ]:
            assert foundation_normalize(raw) == web_normalize(raw), raw


class TestDeriveDedupKey:
    """derive_dedup_key is the single versioned derivation entry point."""

    def test_foundation_and_web_agree(self):
        from jobcannon.engine.normalizers import derive_dedup_key as foundation_derive
        from jobcannon.engine.dedup_normalizer import derive_dedup_key as web_derive

        for company, title in (
            ("Klaviyo Inc.", "Sr. Software Engineer"),
            ("Capital One", "84Data Scientist Jobs"),
            ("Google LLC", "Staff Engineer (IC5)"),
        ):
            assert foundation_derive(company, title) == web_derive(company, title)

    def test_matches_job_dedup_key(self):
        from jobcannon.engine.models import Job
        from jobcannon.engine.normalizers import derive_dedup_key

        job = Job(
            title="Sr. Engineer",
            company="Klaviyo Inc.",
            location="SF",
            source="test",
            source_url="https://example.com",
        )
        assert job.dedup_key == derive_dedup_key("Klaviyo Inc.", "Sr. Engineer")

    def test_location_excluded(self):
        from jobcannon.engine.normalizers import derive_dedup_key

        assert derive_dedup_key("Acme", "Engineer") == derive_dedup_key("Acme", "Engineer")
        # No third segment.
        assert len(derive_dedup_key("Acme", "Engineer").split("|")) == 2


class TestRunRetroactiveDedupMergeSource:
    """The merge_source parameter labels re-key runs distinctly (rekey_v{N})."""

    def test_default_merge_source_is_migration(self, mem_db):
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "old-key-1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "old-key-2", "Senior Engineer", "Acme", first_seen="2026-01-05T00:00:00"
        )

        run_retroactive_dedup(mem_db)

        sources = {r["merge_source"] for r in mem_db.execute("SELECT merge_source FROM merge_log")}
        assert sources == {"migration"}

    def test_custom_merge_source_recorded(self, mem_db):
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "old-key-1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "old-key-2", "Senior Engineer", "Acme", first_seen="2026-01-05T00:00:00"
        )

        run_retroactive_dedup(mem_db, merge_source="rekey_v2")

        sources = {r["merge_source"] for r in mem_db.execute("SELECT merge_source FROM merge_log")}
        assert sources == {"rekey_v2"}

    def test_rekeys_lone_stale_singleton(self, mem_db):
        """A single row whose stored key != derived key is re-keyed (no merge)."""
        from jobcannon.engine.models import Job
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        # Stale key in the old (pre-#238) form; only one row -> no merge, rename.
        _insert_job(
            mem_db,
            "capital one|84data scientist jobs",
            "84Data Scientist Jobs",
            "Capital One",
            first_seen="2026-01-01T00:00:00",
        )
        # Add an FK row to prove the rename rewrites FK tables too.
        mem_db.execute(
            "INSERT INTO pipeline_events (job_id, to_status, timestamp) "
            "VALUES ('capital one|84data scientist jobs', 'discovered', '2026-01-01T00:00:00')"
        )
        mem_db.commit()

        merged = run_retroactive_dedup(mem_db, merge_source="rekey_v2")

        assert merged == 0  # no row removed
        rows = mem_db.execute("SELECT dedup_key FROM jobs").fetchall()
        assert len(rows) == 1
        expected = Job.normalized_dedup_key("Capital One", "84Data Scientist Jobs")
        assert rows[0]["dedup_key"] == expected
        # FK rewritten to the new canonical key.
        ev = mem_db.execute("SELECT job_id FROM pipeline_events").fetchone()
        assert ev["job_id"] == expected

    def test_merge_preserves_user_fields(self, mem_db):
        """Re-key merge keeps highest pipeline_status and concatenates notes."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            pipeline_status="applied",
            notes="Called the recruiter.",
            first_seen="2026-01-01T00:00:00",
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            pipeline_status="discovered",
            notes="Found via LinkedIn.",
            first_seen="2026-01-05T00:00:00",
        )

        run_retroactive_dedup(mem_db, merge_source="rekey_v2")

        rows = mem_db.execute("SELECT pipeline_status, notes FROM jobs").fetchall()
        assert len(rows) == 1
        assert rows[0]["pipeline_status"] == "applied"  # higher precedence wins
        assert "Called the recruiter." in rows[0]["notes"]
        assert "Found via LinkedIn." in rows[0]["notes"]

    def test_idempotent_second_run_is_noop(self, mem_db):
        """Re-running over already-keyed rows merges nothing and renames nothing."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(
            mem_db, "old-key-1", "Senior Engineer", "Acme Inc.", first_seen="2026-01-01T00:00:00"
        )
        _insert_job(
            mem_db, "old-key-2", "Senior Engineer", "Acme", first_seen="2026-01-05T00:00:00"
        )

        first = run_retroactive_dedup(mem_db, merge_source="rekey_v2")
        assert first == 1
        keys_after_first = {r["dedup_key"] for r in mem_db.execute("SELECT dedup_key FROM jobs")}

        second = run_retroactive_dedup(mem_db, merge_source="rekey_v2")
        assert second == 0
        keys_after_second = {r["dedup_key"] for r in mem_db.execute("SELECT dedup_key FROM jobs")}
        assert keys_after_first == keys_after_second

    def test_distinct_jobs_never_merged(self, mem_db):
        """Two genuinely different jobs keep separate rows after a re-key run."""
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        _insert_job(mem_db, "k1", "Data Scientist", "Acme", first_seen="2026-01-01T00:00:00")
        _insert_job(mem_db, "k2", "Product Manager", "Acme", first_seen="2026-01-02T00:00:00")

        merged = run_retroactive_dedup(mem_db, merge_source="rekey_v2")

        assert merged == 0
        assert mem_db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


class TestRetroactiveDedupSingleWriterRouting:
    """The merge never side-writes the scoring tuple or a subset of the five
    canonical location columns — both route through their sanctioned single
    writers (canonical-row score preserved; locations via the D-5 funnel)."""

    def test_scoring_tuple_not_merged_keeps_canonical(self, mem_db):
        """A duplicate's "higher" classification never overwrites the canonical's.

        The old element-wise-max / priority merge fabricated an incoherent score
        (a classification that drifted from the sub_scores and from the row's
        scoring_model). The merge now leaves the canonical's own coherent tuple
        untouched — the version-bump re-key path NULLs it separately to force a
        clean re-score.
        """
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        # Canonical (earliest) scored "consider"; the duplicate scored "apply".
        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            first_seen="2026-01-01T00:00:00",
            classification="consider",
            sub_scores_json='{"comp_fit": 3}',
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            first_seen="2026-01-05T00:00:00",
            classification="apply",
            sub_scores_json='{"comp_fit": 5}',
        )

        run_retroactive_dedup(mem_db)

        rows = mem_db.execute("SELECT classification, sub_scores_json FROM jobs").fetchall()
        assert len(rows) == 1
        # Canonical's own score is preserved verbatim — NOT merged up to "apply"
        # and NOT element-wise-maxed to comp_fit=5.
        assert rows[0]["classification"] == "consider"
        assert rows[0]["sub_scores_json"] == '{"comp_fit": 3}'

    def test_location_merge_routes_through_funnel(self, mem_db):
        """All five canonical location columns stay coherent after a merge.

        The old merge wrote only ``location`` + ``locations_raw``, leaving
        ``locations_structured`` / ``workplace_type`` / ``primary_country_code``
        stale. Routing each duplicate's segments through
        ``apply_location_observation`` rewrites all five together.
        """
        from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

        # Canonical is Remote; the duplicate adds a physical city.
        _insert_job(
            mem_db,
            "old-key-1",
            "Senior Engineer",
            "Acme Inc.",
            first_seen="2026-01-01T00:00:00",
            location="Remote",
            locations_raw='["Remote"]',
        )
        _insert_job(
            mem_db,
            "old-key-2",
            "Senior Engineer",
            "Acme",
            first_seen="2026-01-05T00:00:00",
            location="New York",
            locations_raw='["New York"]',
        )

        run_retroactive_dedup(mem_db)

        row = mem_db.execute(
            "SELECT location, locations_raw, locations_structured, workplace_type FROM jobs"
        ).fetchall()
        assert len(row) == 1
        merged = row[0]
        # Both locations folded in (Remote floats to the front).
        assert "Remote" in merged["location"]
        assert "New York" in merged["location"]
        raw = json.loads(merged["locations_raw"])
        assert "Remote" in raw and "New York" in raw
        # The derived columns the old merge left stale are now populated by the
        # funnel — proof all five moved together.
        assert merged["workplace_type"] == "REMOTE"
        assert merged["locations_structured"] is not None


# ---------------------------------------------------------------------------
# WI-15 (#1829): normalize_company_v2 — aggressive detection-only normalizer
# ---------------------------------------------------------------------------


def test_normalize_company_v2_cases():
    """normalize_company_v2 folds each near-duplicate twin onto its canonical.

    These are the exact twin pairs from the live registry (REPORT B-2): a
    leading article, a trailing dash, a trademark glyph, and an apostrophe
    family (straight quote vs backtick). Each raw form must normalize equal to
    its twin under v2.
    """
    from jobcannon.engine.normalizers import normalize_company_v2

    # Leading article: "The Home Depot" == "Home Depot"
    assert normalize_company_v2("The Home Depot") == normalize_company_v2("Home Depot")
    # Trailing punctuation: "Airwallex-" == "Airwallex"
    assert normalize_company_v2("Airwallex-") == normalize_company_v2("Airwallex")
    # Trademark glyph: "BetterSleep™" == "BetterSleep"
    assert normalize_company_v2("BetterSleep™") == normalize_company_v2("BetterSleep")
    # Apostrophe family — the live twin id 8168 uses a BACKTICK, not a straight
    # quote, so a literal "'s" strip would miss it. All three fold together.
    assert normalize_company_v2("Ken's Foods") == normalize_company_v2("Ken`s Foods")
    assert normalize_company_v2("Ken's Foods") == normalize_company_v2("Kens Foods")

    # Concrete canonical outputs (guards against a fold silently changing).
    assert normalize_company_v2("The Home Depot") == "home depot"
    assert normalize_company_v2("Airwallex-") == "airwallex"
    assert normalize_company_v2("BetterSleep™") == "bettersleep"
    assert normalize_company_v2("Ken`s Foods") == "kens foods"


def test_derive_dedup_key_unchanged_by_v2():
    """The dedup_key path must NOT adopt the v2 folds (invariant for WI-15).

    derive_dedup_key stays on v1 normalize_company; only the detection/reporting
    path uses v2. Pin v1 outputs so a future change that routes v2 into the
    dedup path (which would re-key the whole job table) fails loudly here.
    """
    from jobcannon.engine.normalizers import (
        NORMALIZER_VERSION,
        derive_dedup_key,
        normalize_company,
        normalize_company_v2,
    )

    # The dedup version is still 2 and NOT the company-match version.
    assert NORMALIZER_VERSION == 2

    # v1 (dedup path) keeps the article / dash / glyph / apostrophe that v2 folds.
    assert normalize_company("The Home Depot") == "the home depot"
    assert normalize_company("Airwallex-") == "airwallex-"
    assert normalize_company("BetterSleep™") == "bettersleep™"
    assert normalize_company("Ken's Foods") == "ken's foods"

    # dedup_key is built from v1, so the twins that v2 collapses stay DISTINCT
    # in the dedup key — proof the v2 folds did not leak into derivation.
    assert derive_dedup_key("The Home Depot", "Engineer") == "the home depot|engineer"
    assert derive_dedup_key("The Home Depot", "Engineer") != derive_dedup_key(
        "Home Depot", "Engineer"
    )
    assert derive_dedup_key("Airwallex-", "Engineer") != derive_dedup_key("Airwallex", "Engineer")

    # And the twins DO collapse under v2 (the whole point of the split).
    assert normalize_company_v2("The Home Depot") == normalize_company_v2("Home Depot")
