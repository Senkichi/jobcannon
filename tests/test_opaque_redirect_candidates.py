# PORTED from tests/test_opaque_redirect_candidates.py @ 929e3ad49398f23c4b9e44904f7aeddc62bf6fda (private job-cannon). Ledger L-0518.
"""Tests for derived opaque-redirect candidate helpers."""

from __future__ import annotations

import sqlite3

import pytest

from jobcannon.engine.opaque_redirect_candidates import (
    get_flagged_opaque_redirect_hosts,
    is_opaque_redirect_candidate,
    record_signal0_outcome,
    registrable_host,
)

# PORT-SEAM: bare sqlite3 in-memory table replaces private's migrated_db_path
# fixture -- opaque_redirect_host_outcomes is a local sqlite3 sidecar cache
# (see the module's own docstring), not part of the Postgres schema, so
# there is no db_conn/Postgres equivalent to port to; the DDL below mirrors
# private's migration m205933501_add_opaque_redirect_host_outcomes.py.
_SCHEMA_SQL = (
    "CREATE TABLE opaque_redirect_host_outcomes ("
    "    host TEXT PRIMARY KEY,"
    "    attempts INTEGER NOT NULL DEFAULT 0,"
    "    blocked_count INTEGER NOT NULL DEFAULT 0,"
    "    last_seen TEXT"
    ")"
)


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA_SQL)
    return conn


def _insert_outcome(conn, host, attempts=0, blocked_count=0, last_seen=None):
    conn.execute(
        "INSERT INTO opaque_redirect_host_outcomes (host, attempts, blocked_count, last_seen) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(host) DO UPDATE SET attempts=excluded.attempts, "
        "blocked_count=excluded.blocked_count, last_seen=excluded.last_seen",
        (host, attempts, blocked_count, last_seen),
    )
    conn.commit()


class TestRegistrableHost:
    def test_reduces_subdomain_to_registrable_domain(self):
        assert registrable_host("https://www.jooble.org/away/123") == "jooble.org"

    def test_keeps_bare_two_label_host(self):
        assert registrable_host("https://jooble.org/away/123") == "jooble.org"

    def test_returns_none_for_empty_or_bad_url(self):
        assert registrable_host(None) is None
        assert registrable_host("") is None
        assert registrable_host("://not-a-url") is None


class TestRecordSignal0Outcome:
    @pytest.fixture
    def conn(self):
        # PORT-SEAM: migrated_db_path (full private migration set) replaced
        # with an in-memory DB carrying just this table (see module note above).
        conn = _new_conn()
        yield conn
        conn.close()

    def test_records_attempt_and_blocked(self, conn):
        record_signal0_outcome(conn, "https://jooble.org/away/1", True, True, {"verification": {}})
        row = conn.execute(
            "SELECT * FROM opaque_redirect_host_outcomes WHERE host = ?", ("jooble.org",)
        ).fetchone()
        assert row["attempts"] == 1
        assert row["blocked_count"] == 1
        assert row["last_seen"] is not None

    def test_non_blocked_does_not_increment_blocked_count(self, conn):
        # PORT-SEAM: reflowed onto one line by ruff format -- no semantic change.
        record_signal0_outcome(conn, "https://example.com/job/1", True, False, {"verification": {}})
        row = conn.execute(
            "SELECT * FROM opaque_redirect_host_outcomes WHERE host = ?", ("example.com",)
        ).fetchone()
        assert row["attempts"] == 1
        assert row["blocked_count"] == 0

    def test_skipped_attempt_is_not_recorded(self, conn):
        record_signal0_outcome(
            conn, "https://jooble.org/away/1", False, False, {"verification": {}}
        )
        row = conn.execute(
            "SELECT * FROM opaque_redirect_host_outcomes WHERE host = ?", ("jooble.org",)
        ).fetchone()
        assert row is None

    def test_newly_flagged_returns_true_at_threshold(self, conn):
        config = {
            "verification": {"opaque_derive_min_samples": 3, "opaque_derive_block_ratio": 1.0}
        }
        record_signal0_outcome(conn, "https://bad.example/job/1", True, True, config)
        record_signal0_outcome(conn, "https://bad.example/job/2", True, True, config)
        newly = record_signal0_outcome(conn, "https://bad.example/job/3", True, True, config)
        assert newly is True

    def test_not_newly_flagged_below_threshold(self, conn):
        # Only 2 of min_samples=3 recorded — threshold not reached, newly must be False.
        config = {
            "verification": {"opaque_derive_min_samples": 3, "opaque_derive_block_ratio": 1.0}
        }
        record_signal0_outcome(conn, "https://below.example/job/1", True, True, config)
        newly = record_signal0_outcome(conn, "https://below.example/job/2", True, True, config)
        assert newly is False
        assert (
            is_opaque_redirect_candidate("https://below.example/job/1", conn=conn, config=config)
            is False
        )

    def test_accumulates_across_two_calls(self, conn):
        # Two sequential calls must accumulate; the second call should see attempts=2.
        record_signal0_outcome(
            conn, "https://accum.example/job/1", True, True, {"verification": {}}
        )
        record_signal0_outcome(
            conn, "https://accum.example/job/2", True, False, {"verification": {}}
        )
        row = conn.execute(
            "SELECT attempts, blocked_count FROM opaque_redirect_host_outcomes WHERE host = ?",
            ("accum.example",),
        ).fetchone()
        assert row["attempts"] == 2
        assert row["blocked_count"] == 1

    def test_not_newly_flagged_just_below_ratio(self, conn):
        config = {
            "verification": {"opaque_derive_min_samples": 4, "opaque_derive_block_ratio": 0.8}
        }
        record_signal0_outcome(conn, "https://bad.example/job/1", True, True, config)
        record_signal0_outcome(conn, "https://bad.example/job/2", True, True, config)
        record_signal0_outcome(conn, "https://bad.example/job/3", True, True, config)
        newly = record_signal0_outcome(conn, "https://bad.example/job/4", True, False, config)
        assert newly is False


class TestIsOpaqueRedirectCandidate:
    @pytest.fixture
    def conn(self):
        # PORT-SEAM: migrated_db_path (full private migration set) replaced
        # with an in-memory DB carrying just this table (see module note above).
        conn = _new_conn()
        yield conn
        conn.close()

    def test_false_when_no_outcome(self, conn):
        assert is_opaque_redirect_candidate("https://jooble.org/away/1", conn=conn) is False

    def test_false_when_below_min_samples(self, conn):
        config = {
            "verification": {"opaque_derive_min_samples": 5, "opaque_derive_block_ratio": 0.95}
        }
        _insert_outcome(conn, "jooble.org", attempts=3, blocked_count=3)
        assert (
            is_opaque_redirect_candidate("https://jooble.org/away/1", conn=conn, config=config)
            is False
        )

    def test_false_when_ratio_below_threshold(self, conn):
        config = {
            "verification": {"opaque_derive_min_samples": 4, "opaque_derive_block_ratio": 0.95}
        }
        _insert_outcome(conn, "jooble.org", attempts=4, blocked_count=3)
        assert (
            is_opaque_redirect_candidate("https://jooble.org/away/1", conn=conn, config=config)
            is False
        )

    def test_true_at_threshold(self, conn):
        config = {
            "verification": {"opaque_derive_min_samples": 4, "opaque_derive_block_ratio": 0.75}
        }
        _insert_outcome(conn, "jooble.org", attempts=4, blocked_count=3)
        assert (
            is_opaque_redirect_candidate("https://jooble.org/away/1", conn=conn, config=config)
            is True
        )

    def test_false_when_host_already_in_yaml(self, conn):
        config = {"verification": {"opaque_redirect_sources": [{"domain": "jooble.org"}]}}
        _insert_outcome(conn, "jooble.org", attempts=20, blocked_count=20)
        assert (
            is_opaque_redirect_candidate("https://jooble.org/away/1", conn=conn, config=config)
            is False
        )


class TestGetFlaggedOpaqueRedirectHosts:
    @pytest.fixture
    def conn(self):
        # PORT-SEAM: migrated_db_path (full private migration set) replaced
        # with an in-memory DB carrying just this table (see module note above).
        conn = _new_conn()
        yield conn
        conn.close()

    def test_returns_only_flagged_hosts_not_in_yaml(self, conn):
        _insert_outcome(conn, "jooble.org", attempts=20, blocked_count=19)
        _insert_outcome(conn, "adzuna.com", attempts=20, blocked_count=20)
        _insert_outcome(conn, "example.com", attempts=20, blocked_count=1)
        config = {
            "verification": {
                "opaque_derive_min_samples": 20,
                "opaque_derive_block_ratio": 0.95,
                "opaque_redirect_sources": [{"domain": "adzuna.com"}],
            }
        }
        hosts = get_flagged_opaque_redirect_hosts(conn, config)
        hosts_by_name = {h["host"]: h for h in hosts}
        assert "jooble.org" in hosts_by_name
        assert "adzuna.com" not in hosts_by_name  # already promoted to YAML
        assert "example.com" not in hosts_by_name  # ratio below threshold
        assert hosts_by_name["jooble.org"]["block_ratio"] == 0.95
