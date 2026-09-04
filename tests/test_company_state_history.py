# PORTED from tests/test_company_state_history.py @ f20c5b927308f288888fd068a1d3e7af64b644be (private job-cannon). Ledger L-0040.
"""Tests for WI-08 -- company_state_history append-only audit log.

Covers the migration DDL (schema + idempotency), the sole-writer invariant
(grep guard), the NULL-aware / no-op behaviour of ``record_state_change`` /
``record_state_diff``, and end-to-end wiring: a real UPDATE lands history
rows through ``jobcannon.db._companies.upsert_company`` (ledger L-0040's
injection point), not by calling the helper directly.

# PORT-SEAM: TestReconcileWiring (test_demotion_writes_history_row,
# test_promotion_writes_history_row) dropped entirely -- both drove
# job_finder.web.ats_identity_reconcile.reconcile_company_ats, a private-only
# surface with no public port (no ledger row covers it; not in L-0040's
# scope). TestUpsertCompanyWiring is ADAPTED rather than dropped: it is the
# one wiring test private had for THIS row's injection point, retargeted at
# jobcannon.db._companies.upsert_company (the public single-writer, same
# role as private's job_finder.web.ats_company.upsert_company).
"""

from __future__ import annotations

import re
from pathlib import Path

import jobcannon
from jobcannon.db._company_state import (
    _TRACKED_FIELDS,
    record_state_change,
    record_state_diff,
)
from jobcannon.db.migrations.m0022_company_state_history import MIGRATION

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree, so importing them
# into this module's namespace is what makes pytest discover them here.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres

_EXPECTED_TRACKED = (
    "ats_platform",
    "ats_slug",
    "ats_probe_status",
    "miss_reason",
    "ats_scan_enabled",
    "careers_scan_enabled",
)


def _history(conn, company_id, changed_by=None):
    # PORT-SEAM: private toggled conn.row_factory to sqlite3.Row and back
    # (its default connection factory returns tuples). db_conn already uses
    # psycopg's dict_row factory (tests/host/conftest.py), so every fetched
    # row is already a dict -- no toggling needed.
    sql = (
        "SELECT field, old_value, new_value, changed_by "
        "FROM company_state_history WHERE company_id = %s"
    )
    params = [company_id]
    if changed_by is not None:
        sql += " AND changed_by = %s"
        params.append(changed_by)
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def _insert_company(conn, name):
    # PORT-SEAM: companies.id is a real bigserial PK + company_state_history.
    # company_id is a real FK on this host (unlike private's untyped sqlite3
    # column), so every history row needs a real companies row to reference
    # first -- same idiom as
    # tests/test_migration_209595733_scan_selection_log_and_run_id.py's
    # _insert_company.
    row = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return row["id"]


class TestMigration:
    def test_declares_version(self):
        # PORT-SEAM: 209616158 -> 22 (this host's sequential-integer scheme;
        # see m0022's own module docstring).
        assert MIGRATION.version == 22

    def test_table_and_index_exist(self, db_conn):  # noqa: F811
        # PORT-SEAM: PRAGMA table_info replaced with information_schema.columns;
        # PRAGMA index_list replaced with pg_indexes (this host's established
        # idiom -- see tests/test_migration_209595733_scan_selection_log_and_run_id.py).
        rows = db_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'company_state_history'"
        ).fetchall()
        cols = {r["column_name"] for r in rows}
        assert cols == {
            "id",
            "company_id",
            "field",
            "old_value",
            "new_value",
            "changed_at",
            "changed_by",
        }
        idx = {
            r["indexname"]
            for r in db_conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'company_state_history'"
            ).fetchall()
        }
        assert "idx_company_state_history_company_changed_at" in idx

    def test_tracked_columns_present_on_companies(self, db_conn):  # noqa: F811
        rows = db_conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'companies'"
        ).fetchall()
        company_cols = {r["column_name"] for r in rows}
        assert set(_EXPECTED_TRACKED) <= company_cols
        # The module's tracked set is exactly these six columns. The legacy
        # ``scan_enabled`` aggregate is excluded by WI-13/D16 (no production
        # reads of it); the split flags carry the scan-disable signal instead.
        assert set(_TRACKED_FIELDS) == set(_EXPECTED_TRACKED)
        assert "scan_enabled" not in _TRACKED_FIELDS

    def test_ddl_is_idempotent_against_a_populated_table(self, db_conn):  # noqa: F811
        # PORT-SEAM: private applied the DDL twice against a fresh :memory:
        # connection it fully controlled. db_conn is already a fully-migrated
        # shared Postgres DB (this migration already applied once by
        # postgres_test_dsn), so this test instead re-applies the same
        # IF NOT EXISTS DDL against that already-populated schema and checks
        # a seeded row survives untouched -- same intent (idempotent re-run),
        # adapted to the shared-DB fixture shape.
        conn = db_conn
        company_id = _insert_company(conn, "m0022-idempotent-co")
        conn.execute(
            "INSERT INTO company_state_history (company_id, field, changed_by) VALUES (%s, %s, %s)",
            (company_id, "ats_slug", "t"),
        )
        for stmt in MIGRATION.sql:
            with conn.transaction():
                conn.execute(stmt)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM company_state_history WHERE company_id = %s",
            (company_id,),
        ).fetchone()["n"]
        assert count == 1


class TestSoleWriter:
    def test_record_state_change_is_the_only_insert(self):
        # PORT-SEAM: scans the jobcannon package root instead of job_finder;
        # the `INSERT OR <verb> INTO` alternation is dropped -- Postgres has
        # no `INSERT OR IGNORE`/`INSERT OR REPLACE` syntax, only ON CONFLICT,
        # which does not begin with the literal INSERT INTO this guard scans
        # for and needs no special-casing here.
        pattern = re.compile(r"INSERT\s+INTO\s+company_state_history", re.IGNORECASE)
        root = Path(jobcannon.__file__).parent
        offenders = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == ["db/_company_state.py"], offenders


class TestRecordStateChangeUnit:
    def test_noop_when_equal(self, db_conn):  # noqa: F811
        conn = db_conn
        assert record_state_change(conn, 1, "ats_slug", "x", "x", "t") == 0
        assert record_state_change(conn, 1, "ats_slug", None, None, "t") == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM company_state_history").fetchone()["n"] == 0

    def test_null_aware_transitions(self, db_conn):  # noqa: F811
        conn = db_conn
        company_id = _insert_company(conn, "m0022-null-aware-co")
        assert record_state_change(conn, company_id, "ats_slug", None, "acme", "t") == 1
        # PORT-SEAM: private's ats_scan_enabled/careers_scan_enabled columns
        # didn't exist yet (sqlite untyped 1/0 ints stood in for the
        # not-yet-split flags); this host's columns are real `boolean`
        # (m0021), so the transition is expressed as True/False and
        # `_as_text` coerces it to "True"/"False" rather than private's "1"/"0".
        assert record_state_change(conn, company_id, "ats_scan_enabled", True, False, "t") == 1
        by_field = {r["field"]: r for r in _history(conn, company_id)}
        assert by_field["ats_slug"]["old_value"] is None
        assert by_field["ats_slug"]["new_value"] == "acme"
        assert by_field["ats_scan_enabled"]["old_value"] == "True"
        assert by_field["ats_scan_enabled"]["new_value"] == "False"

    def test_diff_records_nothing_when_a_snapshot_is_none(self, db_conn):  # noqa: F811
        conn = db_conn
        after = dict.fromkeys(_TRACKED_FIELDS)
        assert record_state_diff(conn, 1, None, after, "t") == 0
        assert record_state_diff(conn, 1, after, None, "t") == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM company_state_history").fetchone()["n"] == 0


class TestUpsertCompanyWiring:
    """# PORT-SEAM: retargets private's job_finder.web.ats_company.upsert_company
    at this host's jobcannon.db._companies.upsert_company -- same role
    (single company writer), same injection point this ledger row (L-0040)
    added (jobcannon/db/_companies.py's ``_update_existing``)."""

    def test_insert_path_silent_update_promotion_logged(self, db_conn):  # noqa: F811
        from jobcannon.db._companies import upsert_company
        from jobcannon.db.pool import EngineCompatConnection

        conn = EngineCompatConnection(db_conn)
        cid = upsert_company(conn, "Acme Corp", ats_probe_status="pending")
        assert _history(db_conn, cid) == []  # brand-new INSERT records no history

        upsert_company(
            conn,
            "Acme Corp",
            ats_platform="greenhouse",
            ats_slug="acme",
            ats_probe_status="hit",
        )
        rows = _history(db_conn, cid, changed_by="upsert_company")
        got = {r["field"]: (r["old_value"], r["new_value"]) for r in rows}
        assert got["ats_probe_status"] == ("pending", "hit")
        assert got["ats_platform"] == (None, "greenhouse")
        assert got["ats_slug"] == (None, "acme")

    def test_ats_collision_still_records_untracked_field_no_op(self, db_conn):  # noqa: F811
        # PORT-SEAM: new coverage (not in private's suite) for the collision
        # fallback path this row's docstring calls out explicitly (see
        # jobcannon/db/_companies.py's _update_existing PORT-SEAM comment):
        # a collision retry leaves every tracked field untouched, so the
        # diff after the fallback UPDATE records zero rows even though the
        # untracked homepage_url field DID change.
        from jobcannon.db._companies import upsert_company
        from jobcannon.db.pool import EngineCompatConnection

        conn = EngineCompatConnection(db_conn)
        upsert_company(
            conn, "First Co", ats_platform="greenhouse", ats_slug="shared", ats_probe_status="hit"
        )
        cid_b = upsert_company(conn, "Second Co")
        upsert_company(
            conn,
            "Second Co",
            ats_platform="greenhouse",
            ats_slug="shared",
            homepage_url="https://second.example.com",
        )
        assert _history(db_conn, cid_b, changed_by="upsert_company") == []
