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

        assert normalize_title("84Data Scientist Jobs") == normalize_title("84 Data Scientist Jobs")
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


# DROPPED class (port L-group jobcannon/engine) [TestRunRetroactiveDedup]: row L-0452 (ADAPT) explicitly descopes run_retroactive_dedup and the FK-fold/_update_fk_tables DB-merge helpers -- engine exposes only pure normalize_company/normalized_dedup_key; ported dedup_normalizer.py has no such symbols by design


# ---------------------------------------------------------------------------
# Tests: ALLOWED_FK_TABLES allowlist (DEBT-04)
# ---------------------------------------------------------------------------


# DROPPED class (port L-group jobcannon/engine) [TestAllowlist]: row L-0452 (ADAPT) explicitly descopes run_retroactive_dedup and the FK-fold/_update_fk_tables DB-merge helpers -- engine exposes only pure normalize_company/normalized_dedup_key; ported dedup_normalizer.py has no such symbols by design


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


# DROPPED class (port L-group jobcannon/engine) [TestRunRetroactiveDedupMergeSource]: row L-0452 (ADAPT) explicitly descopes run_retroactive_dedup and the FK-fold/_update_fk_tables DB-merge helpers -- engine exposes only pure normalize_company/normalized_dedup_key; ported dedup_normalizer.py has no such symbols by design


# DROPPED class (port L-group jobcannon/engine) [TestRetroactiveDedupSingleWriterRouting]: row L-0452 (ADAPT) explicitly descopes run_retroactive_dedup and the FK-fold/_update_fk_tables DB-merge helpers -- engine exposes only pure normalize_company/normalized_dedup_key; ported dedup_normalizer.py has no such symbols by design


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
