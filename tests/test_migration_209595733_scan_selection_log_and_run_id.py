# PORTED from tests/test_migration_209595733_scan_selection_log_and_run_id.py @ ec9b1404f684a8f20ad1ec2aa81c3a2f20fc0394 (private job-cannon). Ledger L-0517.
"""Tests for Migration 209595733 — scan_selection_log ledger + run_id column (WI-04, D8).

Covers:
- ``scan_selection_log`` table created with the expected columns.
- The ``decision`` CHECK constraint rejects an unknown value and accepts every
  allowed one.
- ``UNIQUE(run_id, company_id)`` is enforced.
- Both indexes exist.
- ``company_scan_log`` gains a ``run_id`` column.
- Idempotent re-run of the migration statements on a populated DB.

# PORT-SEAM: private's own migrated_db fixture (tempfile + full private
# migration chain) and its tests/helpers/contract_triggers.py
# run_migrations_without_contract bypass are both dropped -- this host's
# tests/host/conftest.py db_conn fixture already yields a connection to a
# session-scoped, fully-migrated Postgres DB (see that module), so there is
# no analogous per-test migration-bypass infrastructure to port; carried_files
# for L-0517 therefore excludes tests/helpers/contract_triggers.py (see PR
# body). The target migration is jobcannon/db/migrations/m0013_scan_selection_log.py
# (this host's sequential-integer renumbering of the private epoch-stamp
# migration; see that module's own docstring), which already drops the
# private table's ``job_id`` column -- ported here for structural parity
# with ledger L-0509's precedent, not a new decision.
"""

from __future__ import annotations

# PORT-SEAM: os/sqlite3/tempfile dropped -- no tmp-file sqlite DB is
# needed; psycopg is needed instead for the Postgres error-type
# assertions below.
import psycopg
import psycopg.errors
import pytest

# PORT-SEAM: private imports MIGRATION from
# job_finder.web.migrations.m209595733_... (rewritten by the
# ported-paths manifest) and tests.helpers.contract_triggers'
# run_migrations_without_contract bypass, which this host has no
# counterpart for -- see module docstring.
from jobcannon.db.migrations.m0013_scan_selection_log import MIGRATION

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree, so importing them
# into this module's namespace is what makes pytest discover them here.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres

_ALLOWED_DECISIONS = (
    "selected",
    "skipped_dormant",
    "skipped_deadline",
    "skipped_disabled",
    "skipped_identity_null",
    "skipped_playwright_excluded",
)


def _insert_company(conn, name):
    # PORT-SEAM: companies.id is a real bigserial PK + company_id is a real
    # FK on this host (unlike private's untyped sqlite3 column), so every
    # scan_selection_log row needs a real companies row to reference first.
    row = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return row["id"]


# PORT-SEAM: tmp_path/sqlite3 migrated_db fixture replaced with the shared,
# already-migrated Postgres db_conn fixture.
def test_table_and_columns(db_conn):  # noqa: F811
    rows = db_conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'scan_selection_log'"
    ).fetchall()
    col_names = {r["column_name"] for r in rows}
    assert col_names == {
        "id",
        "run_id",
        # PORT-SEAM: "job_id" dropped here (see module docstring / L-0509).
        "company_id",
        "decision",
        "tier",
        "rank",
        "created_at",
    }


def test_decision_check_constraint(db_conn):  # noqa: F811
    # PORT-SEAM: migrated_db (tempfile + full private migration chain)
    # replaced with the shared db_conn fixture.
    conn = db_conn
    company_id = _insert_company(conn, "m0013-decision-co")
    # Every allowed decision inserts cleanly.
    for i, decision in enumerate(_ALLOWED_DECISIONS):
        # PORT-SEAM: job_id column/value dropped (see module docstring);
        # company_id is a real FK here so a companies row must exist first.
        conn.execute(
            "INSERT INTO scan_selection_log"
            # PORT-SEAM: job_id/created_at columns dropped from this
            # literal; bind params use %s (see module docstring).
            " (run_id, company_id, decision, tier, rank) VALUES (%s, %s, %s, NULL, NULL)",
            (f"run-ok-{i}", company_id, decision),
        )
    # PORT-SEAM: conn.commit() dropped -- db_conn fixture owns
    # transaction lifecycle for the whole test; explicit commit() is
    # not permitted.

    # An unknown decision is rejected by the CHECK constraint.
    # PORT-SEAM: sqlite3.IntegrityError -> psycopg.errors.CheckViolation.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO scan_selection_log"
            # PORT-SEAM: job_id/created_at columns dropped from this
            # literal; bind params use %s (see module docstring).
            " (run_id, company_id, decision, tier, rank) VALUES ('run-bad', %s, 'bogus', NULL, NULL)",
            (company_id,),
        )


def test_unique_run_company(db_conn):  # noqa: F811
    # PORT-SEAM: migrated_db (tempfile + full private migration chain)
    # replaced with the shared db_conn fixture.
    conn = db_conn
    company_id = _insert_company(conn, "m0013-unique-co")
    # PORT-SEAM: job_id column/value dropped (see module docstring);
    # company_id is a real FK here so a companies row must exist first.
    conn.execute(
        "INSERT INTO scan_selection_log"
        # PORT-SEAM: job_id/created_at columns dropped from this
        # literal; bind params use %s (see module docstring).
        " (run_id, company_id, decision, tier, rank) VALUES ('run-u', %s, 'selected', NULL, 0)",
        (company_id,),
    )
    # PORT-SEAM: conn.commit() dropped -- db_conn fixture owns
    # transaction lifecycle for the whole test; explicit commit() is
    # not permitted.
    # PORT-SEAM: sqlite3.IntegrityError -> psycopg.errors.UniqueViolation.
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO scan_selection_log"
            # PORT-SEAM: job_id/created_at columns dropped from this
            # literal; bind params use %s (see module docstring).
            " (run_id, company_id, decision, tier, rank)"
            " VALUES ('run-u', %s, 'skipped_dormant', NULL, NULL)",
            (company_id,),
        )


def test_indexes_created(db_conn):  # noqa: F811
    # PORT-SEAM: sqlite_master query replaced with pg_indexes (this host's
    # established idiom -- see tests/host/test_m0007_revoked_subjects.py).
    rows = db_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'scan_selection_log'"
    ).fetchall()
    index_names = {row["indexname"] for row in rows}
    assert "idx_ssl_company_created" in index_names
    assert "idx_ssl_run" in index_names


def test_company_scan_log_has_run_id(db_conn):  # noqa: F811
    # PORT-SEAM: PRAGMA table_info(company_scan_log) replaced with
    # information_schema.columns (this host's established idiom).
    rows = db_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'company_scan_log' AND column_name = 'run_id'"
    ).fetchall()
    assert len(rows) == 1


# PORT-SEAM: tmp_path/sqlite3 migrated_db fixture replaced with the
# shared, already-migrated Postgres db_conn fixture.
def test_migration_statements_idempotent(db_conn):  # noqa: F811
    """Re-running the migration's DDL on an already-migrated DB is a no-op.

    The table/index statements are ``IF NOT EXISTS``; the ALTER duplicates a
    column, which the runner swallows in production. Here we assert the guarded
    statements do not raise and the ALTER raises only the expected duplicate.
    """
    # PORT-SEAM: migrated_db (tempfile + full private migration chain)
    # replaced with the shared db_conn fixture.
    conn = db_conn
    for stmt in MIGRATION.sql:
        try:
            # PORT-SEAM: private's loop does a bare per-statement try/except
            # across the whole connection's single transaction (safe under
            # sqlite3's autocommit-by-default connection). db_conn wraps the
            # entire test in ONE outer transaction, and Postgres aborts that
            # whole transaction on the first error a bare execute raises
            # inside it -- any statement run afterward would fail with
            # "transaction is aborted", masking the real assertion. Each
            # statement below therefore runs in its own nested
            # db_conn.transaction() (a SAVEPOINT), so a DuplicateColumn on
            # one statement can't poison the ones that follow it.
            with conn.transaction():
                conn.execute(stmt)
        except psycopg.errors.DuplicateColumn:
            pass
    # PORT-SEAM: conn.commit() dropped -- db_conn fixture owns
    # transaction lifecycle for the whole test; explicit commit() is
    # not permitted.
