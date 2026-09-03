"""Behavior tests for scripts/strip_site_code_prefixes.py (Issue #1046).

Covers the script's safety semantics: dry-run by default, --apply required to
write, scoped only to the 4 confirmed name_raw values (never a table-wide
scan), idempotent re-runs, and collision-skip (never merge/overwrite).
"""

from __future__ import annotations

import sqlite3

from jobcannon.engine.normalizers import normalize_company
from scripts.strip_site_code_prefixes import _CONFIRMED_CASES, main


def _insert_company(conn: sqlite3.Connection, company_id: int, name_raw: str) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, name_raw, created_at, updated_at) "
        "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
        (company_id, normalize_company(name_raw), name_raw),
    )
    conn.commit()


def _fetch(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT id, name, name_raw FROM companies WHERE id = ?", (company_id,)
    ).fetchone()


class TestDryRunDefault:
    def test_no_flag_writes_nothing(self, migrated_db, capsys):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        _insert_company(conn, 1, _CONFIRMED_CASES[0])

        rc = main(["--db", path])

        assert rc == 0
        row = _fetch(conn, 1)
        assert row["name_raw"] == _CONFIRMED_CASES[0]
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_dry_run_explicit_flag_absent_is_still_default(self, migrated_db):
        """--apply is the only opt-in; there is no separate --dry-run flag to omit."""
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        _insert_company(conn, 1, _CONFIRMED_CASES[2])

        main([f"--db={path}"])

        row = _fetch(conn, 1)
        assert row["name_raw"] == _CONFIRMED_CASES[2]


class TestApplyWritesOnlyConfirmedRows:
    def test_apply_writes_all_four_confirmed_rows(self, migrated_db):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        for idx, name_raw in enumerate(_CONFIRMED_CASES, start=1):
            _insert_company(conn, idx, name_raw)
        # An unrelated company that must never be touched.
        _insert_company(conn, 99, "2020 Companies")

        rc = main(["--db", path, "--apply"])

        assert rc == 0
        for idx, name_raw in enumerate(_CONFIRMED_CASES, start=1):
            row = _fetch(conn, idx)
            expected = name_raw.split(" ", 1)[1] if " " in name_raw else name_raw
            # Use the same helper the script uses to avoid duplicating stripping logic.
            from jobcannon.engine.normalizers import strip_site_code_prefix

            assert row["name_raw"] == strip_site_code_prefix(name_raw)
            assert row["name_raw"] != name_raw

        untouched = _fetch(conn, 99)
        assert untouched["name_raw"] == "2020 Companies"

    def test_apply_does_not_touch_unrelated_rows(self, migrated_db):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        _insert_company(conn, 1, _CONFIRMED_CASES[0])
        _insert_company(conn, 2, "84 Lumber")
        _insert_company(conn, 3, "A10 Networks")

        main(["--db", path, "--apply"])

        assert _fetch(conn, 2)["name_raw"] == "84 Lumber"
        assert _fetch(conn, 3)["name_raw"] == "A10 Networks"


class TestIdempotent:
    def test_second_apply_run_is_a_no_op(self, migrated_db, capsys):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        _insert_company(conn, 1, _CONFIRMED_CASES[1])

        main(["--db", path, "--apply"])
        after_first = _fetch(conn, 1)["name_raw"]

        capsys.readouterr()  # clear buffer
        rc = main(["--db", path, "--apply"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "No confirmed site-code rows found" in out
        assert _fetch(conn, 1)["name_raw"] == after_first


class TestCollisionSkip:
    def test_collision_is_reported_and_skipped_not_merged(self, migrated_db, capsys):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        # id=1 is the confirmed site-code row; id=2 already has the stripped name,
        # so applying the strip to id=1 would collide with id=2.
        confirmed = _CONFIRMED_CASES[2]  # "C4000 Stewart Title Company"
        _insert_company(conn, 1, confirmed)
        _insert_company(conn, 2, "Stewart Title Company")

        rc = main(["--db", path, "--apply"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Skipped" in out
        assert "collision" in out
        # Neither row was merged or overwritten.
        assert _fetch(conn, 1)["name_raw"] == confirmed
        assert _fetch(conn, 2)["name_raw"] == "Stewart Title Company"


class TestScopedToConfirmedCasesOnly:
    def test_borderline_names_are_never_matched_by_script(self, migrated_db):
        """The script looks up rows by exact name_raw match against
        _CONFIRMED_CASES -- a company named like the borderline cases is not a
        confirmed case and must never be touched by this script."""
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        _insert_company(conn, 1, "3010 HYDRIL USA DISTRIBUTION")
        _insert_company(conn, 2, "410 ICR United States USA")

        main(["--db", path, "--apply"])

        assert _fetch(conn, 1)["name_raw"] == "3010 HYDRIL USA DISTRIBUTION"
        assert _fetch(conn, 2)["name_raw"] == "410 ICR United States USA"
