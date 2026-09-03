"""One-off admin script: strip site-code prefixes from company names (Issue #1046).

Scoped tightly to the 4 confirmed site-code rows identified in the live-DB
audit (2026-07-10). Does NOT scan or rename the whole companies table, does
NOT call run_retroactive_dedup, and does NOT merge or delete any company row
-- per the issue's invariant ("no company rows deleted or merged in this
issue"). Duplicate-name cleanup is out of scope; that's a detection-only
report (see jobcannon.engine.backfill_companies.find_duplicate_companies_with_job_counts).

Confirmed cases (name_raw values, queried by exact match):
- "0006 MA01-CAMBRIDGE-CROSSING-US4E"
- "0101 The Huntington National Bank"
- "C4000 Stewart Title Company"
- "09516 Banco Nacional de Mexico, S.A., integrante del Grupo Financiero Banamex"

Borderline cases (commented out below -- require owner review; the derived
site-code regex correctly declines to auto-classify these because they lack
a leading-zero or letter+3-digit signal -- see
job_finder/normalizers.py::_SITE_CODE_PREFIX_RE):
- "3010 HYDRIL USA DISTRIBUTION"
- "410 ICR United States USA"

Safety:
- Dry-run by DEFAULT. Pass --apply to write.
- Operates ONLY on companies.name_raw values that exact-match _CONFIRMED_CASES
  -- never a table-wide scan or rename.
- If the stripped name would collide with another existing company (same
  name_raw or same normalized name), that row is reported and skipped --
  never silently overwritten or merged.
- Idempotent: once a row's name_raw has been stripped, it no longer
  exact-matches _CONFIRMED_CASES, so re-running the script is a no-op for it.

Usage:
    uv run python scripts/strip_site_code_prefixes.py [--apply] [--db jobs.db]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from jobcannon.engine.normalizers import normalize_company, strip_site_code_prefix

# Confirmed site-code names from the issue. One-off migration data, not live
# business logic -- see the module docstring for why this is not a
# hardcoded-allowlist violation of the "no embedded name lists" rule.
_CONFIRMED_CASES: list[str] = [
    "0006 MA01-CAMBRIDGE-CROSSING-US4E",
    "0101 The Huntington National Bank",
    "C4000 Stewart Title Company",
    "09516 Banco Nacional de Mexico, S.A., integrante del Grupo Financiero Banamex",
]

# Borderline cases -- commented out, require owner review before any script
# ever touches them:
# "3010 HYDRIL USA DISTRIBUTION"
# "410 ICR United States USA"


def _find_collision(
    conn: sqlite3.Connection, company_id: int, cleaned_name_raw: str
) -> tuple[int, str] | None:
    """Return (id, name_raw) of another company that would collide, or None.

    A collision is another row whose name_raw matches the stripped name
    exactly, or whose normalized name (normalize_company) matches the
    stripped name's normalized form. Either would make the rename ambiguous
    -- the row is reported and skipped rather than merged or overwritten.
    """
    target_norm = normalize_company(cleaned_name_raw)
    for row in conn.execute(
        "SELECT id, name_raw, name FROM companies WHERE id != ?", (company_id,)
    ):
        if row["name_raw"] == cleaned_name_raw or row["name"] == target_norm:
            return row["id"], row["name_raw"]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="jobs.db", help="Path to SQLite database")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this flag the script only reports "
        "what it would do (dry-run is the default).",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # ---- Phase 1: look up the confirmed rows by exact name_raw match -------
    matches: list[tuple[int, str, str]] = []  # (id, name_raw, cleaned)
    for name_raw in _CONFIRMED_CASES:
        rows = conn.execute(
            "SELECT id, name_raw FROM companies WHERE name_raw = ?", (name_raw,)
        ).fetchall()
        for row in rows:
            cleaned = strip_site_code_prefix(row["name_raw"])
            if cleaned == row["name_raw"]:
                continue
            matches.append((row["id"], row["name_raw"], cleaned))

    if not matches:
        print("No confirmed site-code rows found (already cleaned, or not present).")
        conn.close()
        return 0

    print(f"Found {len(matches)} confirmed site-code row(s):")
    for cid, raw, cleaned in matches:
        print(f"  id={cid}: '{raw}' -> '{cleaned}'")

    # ---- Phase 2: collision check ------------------------------------------
    to_update: list[tuple[int, str, str]] = []
    skipped: list[tuple[int, str, str, tuple[int, str]]] = []
    for cid, raw, cleaned in matches:
        collision = _find_collision(conn, cid, cleaned)
        if collision is not None:
            skipped.append((cid, raw, cleaned, collision))
        else:
            to_update.append((cid, raw, cleaned))

    if skipped:
        print(f"\nSkipped {len(skipped)} row(s) due to name collision (no merge performed):")
        for cid, raw, cleaned, (other_id, other_raw) in skipped:
            print(
                f"  id={cid}: '{raw}' -> '{cleaned}' collides with id={other_id} ('{other_raw}')"
            )

    if not to_update:
        print("\nNo rows to update after collision check.")
        conn.close()
        return 0

    if not args.apply:
        print(f"\n[DRY RUN] Would update {len(to_update)} row(s). Pass --apply to write.")
        conn.close()
        return 0

    # ---- Phase 3: apply updates (scoped to the confirmed rows only) -------
    print(f"\nApplying updates to {len(to_update)} row(s)...")
    for cid, raw, cleaned in to_update:
        conn.execute(
            "UPDATE companies SET name_raw = ?, name = ? WHERE id = ?",
            (cleaned, normalize_company(cleaned), cid),
        )
        print(f"  id={cid}: '{raw}' -> '{cleaned}'")
    conn.commit()
    print(f"Updated {len(to_update)} company row(s).")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
