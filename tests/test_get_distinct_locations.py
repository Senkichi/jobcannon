# PORTED from tests/test_get_distinct_locations.py @ 694ac4d08d0f98c322f050b2804894917cdeb64a (private job-cannon). Ledger L-0490.
"""Tests for job_finder.db.get_distinct_locations.

The filter dropdown reads from this helper. It must:
- Source from per-entry locations_raw, NOT the merged location column,
  so multi-location combinations don't bloat the dropdown.
- Apply normalize_location to each entry.
- Lower-case-dedupe so case variants collapse to one entry.
- Return results sorted case-insensitively.

# PORT-SEAM: this host's get_distinct_locations lives in
# jobcannon/db/_scan_observability.py (not job_finder.db) and reads
# postings.locations_raw (jsonb, already-parsed by the driver) rather than
# private's jobs.locations_raw (TEXT column holding a JSON-encoded string
# private json.loads()'d in Python) -- see that module's own PORT-SEAM
# comment. Partial pre-existing coverage already exists in
# tests/host/test_scan_observability.py
# (test_distinct_locations_dedupes_case_insensitively,
# test_distinct_locations_skips_empty_arrays); this file is more thorough
# (placeholder-skip, non-list-scalar skip, sort order, empty DB) and is
# kept per the L-0509 advisor precedent (port despite overlap, note it).
"""

from __future__ import annotations

import json
# PORT-SEAM: sqlite3 dropped -- Postgres via db_conn.

# PORT-SEAM: private imports from job_finder.db (its package __init__
# re-export); this host's get_distinct_locations lives directly in
# jobcannon.db._scan_observability (see that module's own PORT-SEAM note).
from jobcannon.db._scan_observability import get_distinct_locations

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres


def _insert_company(conn, name):
    # PORT-SEAM: companies.id is a real bigserial PK + postings.company_id
    # is a real FK on this host (unlike private's untyped sqlite3 jobs
    # table), so every posting row needs a real companies row first.
    row = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return row["id"]


def _insert(conn, dedup_key: str, locs: list[str]) -> None:
    # PORT-SEAM: company_id is a required real FK on this host, so each
    # call mints its own companies row keyed off dedup_key.
    company_id = _insert_company(conn, f"loc-{dedup_key}")
    conn.execute(
        "INSERT INTO postings"
        # PORT-SEAM: private's jobs(...) columns replaced with this host's
        # postings(...) columns; locations_raw is still written via
        # json.dumps() (this host's established jsonb-write idiom -- see
        # tests/host/test_scan_observability.py _seed_posting).
        " (dedup_key, company_id, title, company, locations_raw)"
        " VALUES (%s, %s, 'Engineer', 'X', %s)",
        (dedup_key, company_id, json.dumps(locs)),
    )
    # PORT-SEAM: conn.commit() dropped -- db_conn fixture owns transaction
    # lifecycle for the whole test; explicit commit() is not permitted.


class TestDistinctLocations:
    # PORT-SEAM: tmp_path/sqlite3 migrated_db_mem fixture replaced with the
    # shared, already-migrated Postgres db_conn fixture throughout this
    # class (fixture param renamed on every test method below).
    def test_returns_individual_entries_not_merged_combinations(self, db_conn):  # noqa: F811
        """Two jobs with overlapping location sets should produce a clean
        set of individual entries, not a separate entry per multi-location
        combination."""
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        _insert(conn, "j1", ["Remote", "NYC"])
        _insert(conn, "j2", ["Remote", "SF"])
        _insert(conn, "j3", ["NYC", "SF", "Remote"])
        result = get_distinct_locations(conn)
        assert sorted(result, key=str.lower) == ["NYC", "Remote", "SF"]

    def test_case_insensitively_dedupes(self, db_conn):  # noqa: F811
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        _insert(conn, "j1", ["Remote"])
        _insert(conn, "j2", ["remote"])
        _insert(conn, "j3", ["REMOTE"])
        result = get_distinct_locations(conn)
        assert len(result) == 1
        # Display uses the first-seen casing
        assert result[0].lower() == "remote"

    def test_skips_placeholder_entries(self, db_conn):  # noqa: F811
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        _insert(conn, "j1", ["Unknown"])
        _insert(conn, "j2", ["TBD", "San Francisco, CA"])
        _insert(conn, "j3", ["N/A"])
        result = get_distinct_locations(conn)
        assert result == ["San Francisco, CA"]

    def test_handles_empty_locations_raw_gracefully(self, db_conn):  # noqa: F811
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        _insert(conn, "j1", [])
        _insert(conn, "j2", ["Remote"])
        result = get_distinct_locations(conn)
        assert result == ["Remote"]

    # PORT-SEAM: renamed from test_skips_invalid_json -- postings.locations_raw
    # is jsonb (validated at WRITE time by Postgres), so a syntactically
    # malformed value like private's 'not-json' TEXT literal can no longer
    # reach a stored row at all. The closest read-time-reachable equivalent
    # of "skip malformed content" is a syntactically-valid jsonb value that
    # is not a list (get_distinct_locations's own `if not isinstance(locs,
    # list): continue` guard), exercised directly below.
    def test_skips_non_list_locations_raw(self, db_conn):  # noqa: F811
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        # Directly insert a row with a non-list (but valid-JSON) locations_raw
        company_id = _insert_company(conn, "loc-bad-type-co")
        conn.execute(
            # PORT-SEAM: private's malformed-JSON TEXT literal against the
            # jobs table replaced with a syntactically-valid non-list jsonb
            # value against postings (see comment above this test's def
            # line).
            "INSERT INTO postings"
            " (dedup_key, company_id, title, company, locations_raw)"
            " VALUES ('j_bad', %s, 'Engineer', 'X', %s)",
            (company_id, json.dumps("not-a-list")),
        )
        _insert(conn, "j_good", ["Remote"])
        # PORT-SEAM: conn.commit() dropped -- db_conn fixture owns
        # transaction lifecycle for the whole test; explicit commit() is
        # not permitted.
        result = get_distinct_locations(conn)
        assert result == ["Remote"]  # malformed row silently skipped

    def test_returns_sorted_case_insensitively(self, db_conn):  # noqa: F811
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        _insert(conn, "j1", ["zurich"])
        _insert(conn, "j2", ["Atlanta"])
        _insert(conn, "j3", ["boston"])
        result = get_distinct_locations(conn)
        # Lower-case alpha sort: atlanta, boston, zurich
        assert [v.lower() for v in result] == ["atlanta", "boston", "zurich"]

    def test_empty_db_returns_empty_list(self, db_conn):  # noqa: F811
        conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
        assert get_distinct_locations(conn) == []
