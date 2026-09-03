# PORTED from tests/test_surfaced_concentration.py @ 3d5b9c72fdbdd29f7a8381c06364a9aaf001cbe1 (private job-cannon). Ledger L-0511.
"""Tests for surfaced concentration metrics (issue #592).

# PORT-SEAM: test_surfaced_concentration_null_company_id dropped -- this
# host's postings.company_id is `bigint NOT NULL REFERENCES companies(id)`
# (m0001), so the private test's premise (a surfaced posting with a NULL
# company_id, exercising the '_unlinked' sentinel) cannot occur here. See
# jobcannon/db/_scan_observability.py's own get_surfaced_concentration
# docstring: "the '_unlinked' sentinel branch is retained for structural
# parity with private ... but is currently unreachable on this host."
"""

from __future__ import annotations

import pytest

from jobcannon.db._scan_observability import (
    _normalized_hhi,
    _shannon_entropy,
    get_surfaced_concentration,
)

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree, so importing them
# into this module's namespace is what makes pytest discover them here.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres


def _insert_company(conn, name, ats_platform=None):
    # PORT-SEAM: replaces private's sqlite3 "INSERT INTO companies (id, ats_platform)
    # VALUES (?, ?)" with a caller-chosen text id -- this host's companies.id is a
    # bigserial PK, so callers get an id back via RETURNING instead of choosing one.
    row = conn.execute(
        "INSERT INTO companies (name, ats_platform) VALUES (%s, %s) RETURNING id",
        (name, ats_platform),
    ).fetchone()
    return row["id"]


def _insert_posting(conn, dedup_key, company_id, classification):
    # PORT-SEAM: replaces private's sqlite3 "INSERT INTO jobs (dedup_key, company_id,
    # classification) VALUES (?, ?, ?)" -- jobs -> postings, adds the title/company
    # NOT NULL columns postings requires; company_id is a real FK here (see module
    # docstring) so the owning company must already exist.
    conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, classification) "
        "VALUES (%s, %s, %s, %s, %s)",
        (dedup_key, company_id, "Test Job", "TestCo", classification),
    )


def test_normalized_hhi_edge_cases():
    """Test normalized HHI edge cases."""
    # Empty list -> None
    assert _normalized_hhi([]) is None

    # Single group -> 1.0
    assert _normalized_hhi([10]) == 1.0

    # Two groups, even split -> 0.0
    assert _normalized_hhi([5, 5]) == pytest.approx(0.0, abs=0.01)

    # Two groups, 60/40 split
    # p1=0.6, p2=0.4, sum_p_squared=0.36+0.16=0.52
    # normalized = (0.52 - 0.5) / (1 - 0.5) = 0.02 / 0.5 = 0.04
    result = _normalized_hhi([6, 4])
    assert result == pytest.approx(0.04, abs=0.01)

    # Perfect concentration (one group holds everything)
    assert _normalized_hhi([10, 0, 0]) == 1.0


def test_shannon_entropy_edge_cases():
    """Test Shannon entropy edge cases."""
    # Empty list -> None
    assert _shannon_entropy([]) is None

    # Single group -> None (log2(1) = 0, would divide by zero)
    assert _shannon_entropy([10]) is None

    # Two groups, even split -> max entropy
    entropy, normalized = _shannon_entropy([5, 5])
    assert entropy == pytest.approx(1.0, abs=0.01)
    assert normalized == pytest.approx(1.0, abs=0.01)

    # Two groups, 60/40 split
    entropy, normalized = _shannon_entropy([6, 4])
    # H = -0.6*log2(0.6) - 0.4*log2(0.4) ≈ 0.971
    assert entropy == pytest.approx(0.971, abs=0.01)
    assert normalized == pytest.approx(0.971, abs=0.01)


# PORT-SEAM: tmp_path/sqlite3 replaced with the shared Postgres db_conn fixture.
def test_surfaced_concentration_synthetic_cohort(db_conn):  # noqa: F811
    """Test concentration metrics with synthetic surfaced cohort."""
    conn = db_conn
    # PORT-SEAM: companies inserted before postings (real FK on this host,
    # unlike private's untyped sqlite3 columns) -- 5 companies, 3 greenhouse + 2 lever.
    company_ids = [
        _insert_company(conn, f"Company {i}", "greenhouse" if i < 3 else "lever") for i in range(5)
    ]

    # Insert 10 surfaced jobs evenly across 5 employers
    for i in range(10):
        # PORT-SEAM: per-job sqlite3 INSERT + separate companies-with-platforms
        # loop collapsed into _insert_posting against the already-inserted
        # company_ids (real FK, companies must exist first on this host).
        _insert_posting(conn, f"job_{i}", company_ids[i % 5], "apply")

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Employer grouping: 5 groups, 2 jobs each -> very low HHI
    assert result["by_employer"]["total"] == 10
    assert result["by_employer"]["n_groups"] == 5
    assert result["by_employer"]["hhi"] is not None
    assert result["by_employer"]["hhi"] < 0.05  # Nearly even

    # Platform grouping: 2 platforms (3 greenhouse, 2 lever)
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 2
    assert result["by_platform"]["hhi"] is not None
    # 60/40 split -> HHI ≈ 0.04
    assert result["by_platform"]["hhi"] == pytest.approx(0.04, abs=0.01)


# PORT-SEAM: tmp_path/sqlite3 replaced with the shared Postgres db_conn fixture.
def test_surfaced_concentration_concentrated_cohort(db_conn):  # noqa: F811
    """Test concentration metrics with concentrated cohort."""
    conn = db_conn
    # PORT-SEAM: company inserted before postings (real FK, see above).
    company_id = _insert_company(conn, "Company 0", "greenhouse")

    # Insert 10 surfaced jobs all on ONE employer
    for i in range(10):
        # PORT-SEAM: per-job sqlite3 INSERT + separate company-with-platform
        # insert collapsed into _insert_posting against the already-inserted
        # company_id (real FK, company must exist first on this host).
        _insert_posting(conn, f"job_{i}", company_id, "apply")

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Employer grouping: single group -> HHI = 1.0
    assert result["by_employer"]["total"] == 10
    assert result["by_employer"]["n_groups"] == 1
    assert result["by_employer"]["hhi"] == 1.0

    # Platform grouping: single platform -> HHI = 1.0
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 1
    assert result["by_platform"]["hhi"] == 1.0


# PORT-SEAM: tmp_path/sqlite3 replaced with the shared Postgres db_conn fixture.
def test_surfaced_concentration_excludes_non_surfaced(db_conn):  # noqa: F811
    """Test that non-surfaced rows (skip/reject/low_signal) are excluded."""
    conn = db_conn
    # PORT-SEAM: 10 companies inserted before postings (real FK, see above).
    company_ids = [_insert_company(conn, f"Company {i}", "greenhouse") for i in range(10)]

    # Insert 5 surfaced jobs (apply/consider)
    for i in range(5):
        classification = "apply" if i < 3 else "consider"
        # PORT-SEAM: sqlite3 INSERT INTO jobs replaced with _insert_posting
        # against the already-inserted company_ids (real FK, see above).
        _insert_posting(conn, f"job_{i}", company_ids[i], classification)

    # Insert 5 non-surfaced jobs (skip/reject/low_signal)
    for i in range(5, 10):
        classification = "skip" if i < 7 else ("reject" if i < 9 else "low_signal")
        # PORT-SEAM: sqlite3 INSERT INTO jobs replaced with _insert_posting
        # against the already-inserted company_ids (real FK, see above).
        _insert_posting(conn, f"job_{i}", company_ids[i], classification)

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Only surfaced jobs should be counted
    assert result["by_employer"]["total"] == 5
    assert result["by_employer"]["n_groups"] == 5


# PORT-SEAM: tmp_path/sqlite3 replaced with the shared Postgres db_conn fixture.
def test_surfaced_concentration_null_platform(db_conn):  # noqa: F811
    """Test that NULL/empty platform is folded into _unknown sentinel."""
    # PORT-SEAM: db_path/sqlite3.connect/row_factory/CREATE TABLE setup
    # dropped -- db_conn fixture already owns a migrated Postgres schema.
    conn = db_conn

    # Insert 5 surfaced jobs with companies that have NULL platform
    for i in range(5):
        # PORT-SEAM: per-iteration company insert replaces private's two-phase
        # jobs-then-companies loops -- companies must exist before postings
        # reference them (real FK, see module docstring).
        company_id = _insert_company(conn, f"Company {i}", None)
        _insert_posting(conn, f"job_{i}", company_id, "apply")

    # Insert 5 surfaced jobs with companies that have real platform
    for i in range(5, 10):
        # PORT-SEAM: per-iteration company insert, see above.
        company_id = _insert_company(conn, f"Company {i}", "greenhouse")
        _insert_posting(conn, f"job_{i}", company_id, "apply")

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Should have 2 groups: _unknown + greenhouse
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 2


# PORT-SEAM: tmp_path/sqlite3 replaced with the shared Postgres db_conn fixture.
def test_surfaced_concentration_empty_platform(db_conn):  # noqa: F811
    """Test that empty string platform is folded into _unknown sentinel."""
    # PORT-SEAM: db_path/sqlite3.connect/row_factory/CREATE TABLE setup
    # dropped -- db_conn fixture already owns a migrated Postgres schema.
    conn = db_conn

    # Insert 5 surfaced jobs with companies that have empty platform
    for i in range(5):
        # PORT-SEAM: per-iteration company insert replaces private's two-phase
        # jobs-then-companies loops -- companies must exist before postings
        # reference them (real FK, see module docstring).
        company_id = _insert_company(conn, f"Company {i}", "")
        _insert_posting(conn, f"job_{i}", company_id, "apply")

    # Insert 5 surfaced jobs with companies that have real platform
    for i in range(5, 10):
        # PORT-SEAM: per-iteration company insert, see above.
        company_id = _insert_company(conn, f"Company {i}", "greenhouse")
        _insert_posting(conn, f"job_{i}", company_id, "apply")

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Should have 2 groups: _unknown + greenhouse
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 2


# PORT-SEAM: tmp_path/sqlite3 replaced with the shared Postgres db_conn fixture.
def test_surfaced_concentration_zero_total(db_conn):  # noqa: F811
    """Test that zero surfaced jobs returns None for HHI/entropy."""
    # PORT-SEAM: db_path/sqlite3.connect/row_factory/CREATE TABLE setup
    # dropped -- db_conn fixture already owns a migrated Postgres schema.
    conn = db_conn

    # No surfaced jobs
    result = get_surfaced_concentration(conn)

    assert result["by_employer"]["total"] == 0
    assert result["by_employer"]["n_groups"] == 0
    assert result["by_employer"]["hhi"] is None
    assert result["by_employer"]["entropy"] is None
    assert result["by_employer"]["entropy_norm"] is None

    assert result["by_platform"]["total"] == 0
    assert result["by_platform"]["n_groups"] == 0
    assert result["by_platform"]["hhi"] is None
    assert result["by_platform"]["entropy"] is None
    assert result["by_platform"]["entropy_norm"] is None
