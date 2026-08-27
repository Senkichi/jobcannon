"""upsert_profile / get_profile — the first (and only) `profiles` writer
(1B Wave 3 PR 11). `profiles` had no writer anywhere on merged main
(Reconciliation Preamble item 6), so it starts life single-writer by
construction — no AST-scanned guard test is needed the way `events` has one
(tests/host/test_events_single_writer.py): there is only one call site to
begin with.

`GUEST_USER_ID` is defined ONCE here — `jobcannon/web/pages.py` and
`scripts/seed_guest_demo.py` both import it rather than re-declaring the
literal.

Single current row: `profiles.user_id` is the PRIMARY KEY (m0001), so
`upsert_profile` is INSERT ... ON CONFLICT (user_id) DO UPDATE, never a
second row per user. Each column update uses `COALESCE(EXCLUDED.col,
profiles.col)` so an omitted (None) keyword argument preserves the
previous value instead of clobbering it with NULL — callers pass only the
fields they actually collected (there is no raw-resume field to store; the
parse-and-discard design means only structured fields ever reach this
table).

jsonb columns (`skills`, `target_titles`, `target_locations`, `target_companies`)
are wrapped in psycopg's `Jsonb` adapter when not None, matching every other
jsonb write in this codebase (_companies.py / _jobs.py / _jd_full.py /
_events.py / health_recorder.py) rather than pre-serialized with
json.dumps().

`target_companies` (m0011, #169/#170) follows `target_titles`'s exact
COALESCE-preserve-when-omitted shape, with one caller-side difference that
matters: `jobcannon/web/onboarding.py`'s `start_submit` passes the picker's
selection list LITERALLY (`selections["titles"]`/`selections["companies"]`),
never coerced to None when empty. An empty list is a real, non-NULL jsonb
value (`Jsonb([])`), so COALESCE picks the NEW (empty) value over the OLD
one — an unchecked-everything resubmission actually clears the stored
filter. Coercing an empty selection to None instead (as this call site used
to for target_titles, before #169) would make COALESCE preserve the STALE
value, silently reviving a filter the visitor just tried to remove — the
one shape of "omitted field" this table's COALESCE design was never meant
to represent, since the picker always submits a complete snapshot of every
field, never a partial patch.

`workplace_type` (m0011) is the one exception to the COALESCE-preserve
pattern: it is a single nullable `text` column whose only valid "no
preference" value (NULL, mapped from the picker's "any" option — see
`onboarding.py`'s `_WORKPLACE_FILTERS`) is indistinguishable from "omitted"
under COALESCE, which would make reverting to "any" from a specific
preference impossible. Both current callers (`start_submit`,
`jobcannon/web/handoff.py`'s re-key) always submit the field's true current
value on every call, so a plain (non-COALESCE) overwrite is both correct
and simpler here — there is no caller today that needs "leave this column
alone."

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
    comp_floor_usd: int | None = None,
    target_companies: list | None = None,
    workplace_type: str | None = None,
) -> None:
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO profiles (user_id, skills, experience_summary, target_titles, "
        "target_locations, seniority_level, years_of_experience, comp_floor_usd, "
        "target_companies, workplace_type, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "skills = COALESCE(EXCLUDED.skills, profiles.skills), "
        "experience_summary = COALESCE(EXCLUDED.experience_summary, profiles.experience_summary), "
        "target_titles = COALESCE(EXCLUDED.target_titles, profiles.target_titles), "
        "target_locations = COALESCE(EXCLUDED.target_locations, profiles.target_locations), "
        "seniority_level = COALESCE(EXCLUDED.seniority_level, profiles.seniority_level), "
        "years_of_experience = COALESCE(EXCLUDED.years_of_experience, profiles.years_of_experience), "
        "comp_floor_usd = COALESCE(EXCLUDED.comp_floor_usd, profiles.comp_floor_usd), "
        "target_companies = COALESCE(EXCLUDED.target_companies, profiles.target_companies), "
        # Plain overwrite, deliberately NOT COALESCE — see this module's
        # docstring for why workplace_type is the one column that must
        # always take the caller's literal value, including NULL.
        "workplace_type = EXCLUDED.workplace_type, "
        "updated_at = now()",
        (
            user_id,
            Jsonb(skills) if skills is not None else None,
            experience_summary,
            Jsonb(target_titles) if target_titles is not None else None,
            Jsonb(target_locations) if target_locations is not None else None,
            seniority_level,
            years_of_experience,
            comp_floor_usd,
            Jsonb(target_companies) if target_companies is not None else None,
            workplace_type,
        ),
    )
    commit_unless_nested(raw)


def get_profile(conn: Any, user_id: str) -> Any:
    """Explicit column list (not `SELECT *`, #105): `jobcannon/web/export.py`'s
    self-service account export is a direct consumer of this row via
    `_row_to_dict`, and a data-minimization decision — does a new `profiles`
    column belong in that export? — must be made consciously here, not
    inherited automatically the moment a migration adds the column.
    `tests/host/test_account_export.py` pins the export document's per-section
    key sets (including this one) so a schema change that silently widens
    either this function or the export fails loudly instead of passing."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute(
        "SELECT user_id, skills, experience_summary, target_titles, target_locations, "
        "seniority_level, years_of_experience, comp_floor_usd, target_companies, "
        "workplace_type, updated_at "
        "FROM profiles WHERE user_id = %s",
        (user_id,),
    ).fetchone()
