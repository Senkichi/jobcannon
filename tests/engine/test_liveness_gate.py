"""Tests for the liveness gate for second-tier aggregators.

Ported from tests/test_liveness_gate.py's TestIsGatedSource class only — the
pure registry-level predicate is_gated_source, which has zero DB/migration
dependency (one test below feeds it an in-memory sqlite3.Row for input-shape
coverage, but needs no schema or migrations to do so).

The source file's other three classes (TestLivenessGateSQLWiring,
TestGateHiddenBySourceParity, TestIsGatedSourceSQLParity) exercise
job_finder.db.get_filtered_jobs, job_finder.db.get_liveness_stats, and
job_finder.db._queries._liveness_gate_sql. None of that job_finder.db query
layer exists in the engine — source_registry.py was ported for this PR,
job_finder.db's query layer was not — so those three classes are NOT ported.
This is a deliberate, documented scope exclusion (out of this PR's declared
module scope), not a coverage regression of anything already covered here.
"""

from __future__ import annotations

import sqlite3

from jobcannon.engine.source_registry import is_gated_source


class TestIsGatedSource:
    """Tests for the registry-level is_gated_source function."""

    def test_jooble_only_is_gated(self):
        """Job with only jooble source is gated when jooble is in gated_sources."""
        config = {"verification": {"gated_sources": ["portal_jooble"]}}
        job = {"sources": ["portal_jooble"], "source_urls": ["https://jooble.org/away/1"]}
        assert is_gated_source(job, config)

    def test_jooble_plus_linkedin_not_gated(self):
        """Job with jooble + linkedin is NOT gated (corroboration wins)."""
        config = {"verification": {"gated_sources": ["portal_jooble"]}}
        job = {
            "sources": ["portal_jooble", "linkedin"],
            "source_urls": ["https://jooble.org/away/1", "https://linkedin.com/jobs/1"],
        }
        assert not is_gated_source(job, config)

    def test_non_gated_source_not_gated(self):
        """Job with only non-gated source is not gated."""
        config = {"verification": {"gated_sources": ["portal_jooble"]}}
        job = {"sources": ["linkedin"], "source_urls": ["https://linkedin.com/jobs/1"]}
        assert not is_gated_source(job, config)

    def test_empty_gated_sources_not_gated(self):
        """When gated_sources is empty, nothing is gated."""
        config = {"verification": {"gated_sources": []}}
        job = {"sources": ["portal_jooble"], "source_urls": ["https://jooble.org/away/1"]}
        assert not is_gated_source(job, config)

    def test_multiple_gated_sources_all_gated(self):
        """Job with multiple gated sources is gated when all are in the list."""
        config = {"verification": {"gated_sources": ["portal_jooble", "portal_adzuna"]}}
        job = {
            "sources": ["portal_jooble", "portal_adzuna"],
            "source_urls": ["https://jooble.org/away/1", "https://adzuna.com/details/1"],
        }
        assert is_gated_source(job, config)

    def test_multiple_gated_sources_one_not_gated(self):
        """Job with multiple sources where one is not gated is not gated."""
        config = {"verification": {"gated_sources": ["portal_jooble"]}}
        job = {
            "sources": ["portal_jooble", "portal_adzuna"],
            "source_urls": ["https://jooble.org/away/1", "https://adzuna.com/details/1"],
        }
        assert not is_gated_source(job, config)

    def test_tolerates_json_string_columns(self):
        """Handles JSON string columns from the DB."""
        config = {"verification": {"gated_sources": ["portal_jooble"]}}
        job = {"sources": '["portal_jooble"]', "source_urls": '["https://jooble.org/away/1"]'}
        assert is_gated_source(job, config)

    def test_sqlite_row_input(self):
        """Handles sqlite3.Row objects."""
        config = {"verification": {"gated_sources": ["portal_jooble"]}}
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (sources TEXT, source_urls TEXT)")
        conn.execute(
            "INSERT INTO t VALUES (?, ?)",
            ('["portal_jooble"]', '["https://jooble.org/away/1"]'),
        )
        row = conn.execute("SELECT * FROM t").fetchone()
        assert is_gated_source(row, config)
        conn.close()
