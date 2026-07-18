"""upsert_profile / get_profile — the first (and only) `profiles` writer
(1B Wave 3 PR 11). `profiles` had no writer anywhere on merged main
(Reconciliation Preamble item 6), so it starts life single-writer by
construction — no AST-scanned guard test is needed the way `events` has one
(tests/host/test_events_single_writer.py): there is only one call site to
begin with.

`GUEST_USER_ID` is defined ONCE here — `jobcannon/web/pages.py` and
`scripts/seed_guest_demo.py` both import it rather than re-declaring the
literal.

Single current row (OD-5): `profiles.user_id` is the PRIMARY KEY (m0001), so
`upsert_profile` is INSERT ... ON CONFLICT (user_id) DO UPDATE, never a
second row per user. Each column update uses `COALESCE(EXCLUDED.col,
profiles.col)` so an omitted (None) keyword argument preserves the
previous value instead of clobbering it with NULL — callers pass only the
fields they actually collected (there is no raw-resume field to store; the
parse-and-discard design means only structured fields ever reach this
table).

jsonb columns (`skills`, `target_titles`, `target_locations`) are wrapped in
psycopg's `Jsonb` adapter when not None, matching every other jsonb write in
this codebase (_companies.py / _jobs.py / _jd_full.py / _events.py /
health_recorder.py) rather than pre-serialized with json.dumps().

Row access: STRING-KEY only (Reconciliation Preamble item 12) — `get_profile`
returns the row mapping as-is (both the pooled hybrid_row and the test
fixtures' dict_row support `row["col"]`).

`commit_unless_nested` (mirrors _companies.py / _jobs.py / _jd_full.py):
`upsert_profile` is called BOTH through a bare pooled connection (the seed
script — a real .commit() is required) AND directly against
tests/host/conftest.py's rollback-isolated `db_conn` fixture (an explicit
.commit() is forbidden there and the no-op path applies)."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from jobcannon.db.pool import commit_unless_nested

GUEST_USER_ID = "guest_demo"


def upsert_profile(
    conn: Any,
    user_id: str,
    *,
    skills: list | None = None,
    experience_summary: str | None = None,
    target_titles: list | None = None,
    target_locations: list | None = None,
    seniority_level: str | None = None,
    years_of_experience: float | None = None,
) -> None:
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO profiles (user_id, skills, experience_summary, target_titles, "
        "target_locations, seniority_level, years_of_experience, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "skills = COALESCE(EXCLUDED.skills, profiles.skills), "
        "experience_summary = COALESCE(EXCLUDED.experience_summary, profiles.experience_summary), "
        "target_titles = COALESCE(EXCLUDED.target_titles, profiles.target_titles), "
        "target_locations = COALESCE(EXCLUDED.target_locations, profiles.target_locations), "
        "seniority_level = COALESCE(EXCLUDED.seniority_level, profiles.seniority_level), "
        "years_of_experience = COALESCE(EXCLUDED.years_of_experience, profiles.years_of_experience), "
        "updated_at = now()",
        (
            user_id,
            Jsonb(skills) if skills is not None else None,
            experience_summary,
            Jsonb(target_titles) if target_titles is not None else None,
            Jsonb(target_locations) if target_locations is not None else None,
            seniority_level,
            years_of_experience,
        ),
    )
    commit_unless_nested(raw)


def get_profile(conn: Any, user_id: str) -> Any:
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,)).fetchone()
