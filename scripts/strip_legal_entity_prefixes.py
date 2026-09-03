"""One-shot migration: strip legal-entity code prefixes from existing rows.

Updates:
- jobs.company  (display value)
- companies.name_raw  (display value + source of company_name used by ATS scans)

After updating display values, re-runs run_retroactive_dedup to merge any
new dedup collisions created by the cleanup (e.g. existing "GE Healthcare"
rows colliding with newly-cleaned "HC1316 GE Precision Healthcare LLC"
rows).

Idempotent: rows already clean are skipped.

Usage:
    uv run python scripts/strip_legal_entity_prefixes.py [--dry-run] [--db jobs.db]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from jobcannon.engine.normalizers import normalize_company, strip_legal_entity_prefix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="jobs.db", help="Path to SQLite database")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying the database",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # ---- Phase 1: jobs.company ------------------------------------------------
    job_updates: list[tuple[str, str]] = []  # (new_company, dedup_key)
    for row in conn.execute("SELECT dedup_key, company FROM jobs WHERE company IS NOT NULL"):
        cleaned = strip_legal_entity_prefix(row["company"])
        if cleaned != row["company"]:
            job_updates.append((cleaned, row["dedup_key"]))

    print(f"jobs.company: {len(job_updates)} rows to update")
    for new, dk in job_updates[:10]:
        print(f"  '{dk[:60]}...' -> company='{new}'")
    if len(job_updates) > 10:
        print(f"  ... and {len(job_updates) - 10} more")

    # ---- Phase 2: companies.name_raw -----------------------------------------
    company_updates: list[tuple[str, int]] = []  # (new_name_raw, id)
    for row in conn.execute("SELECT id, name_raw FROM companies WHERE name_raw IS NOT NULL"):
        cleaned = strip_legal_entity_prefix(row["name_raw"])
        if cleaned != row["name_raw"]:
            company_updates.append((cleaned, row["id"]))

    print(f"companies.name_raw: {len(company_updates)} rows to update")
    for new, cid in company_updates[:10]:
        print(f"  id={cid} -> name_raw='{new}'")
    if len(company_updates) > 10:
        print(f"  ... and {len(company_updates) - 10} more")

    if args.dry_run:
        print("\n[DRY RUN] No changes written.")
        return 0

    # ---- Phase 3: apply -------------------------------------------------------
    conn.executemany(
        "UPDATE jobs SET company = ? WHERE dedup_key = ?",
        job_updates,
    )
    conn.executemany(
        "UPDATE companies SET name_raw = ? WHERE id = ?",
        company_updates,
    )
    conn.commit()
    print(f"\nUpdated {len(job_updates)} jobs + {len(company_updates)} companies.")

    # ---- Phase 4: refresh companies.name (normalized lookup field) -----------
    # name_raw is the display value; name is the lowercased normalized key
    # used by upsert_company for lookup. After cleaning name_raw, the name
    # must be re-derived or future upsert paths will create duplicate rows.
    # Collisions (another company already owns the cleaned name) are left
    # alone — the duplicates can be manually consolidated.
    name_updates: list[tuple[str, int]] = []
    collisions: list[tuple[int, str, str, list[int]]] = []
    by_name: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, name FROM companies"):
        by_name.setdefault(row["name"], []).append(row["id"])
    for row in conn.execute("SELECT id, name, name_raw FROM companies WHERE name_raw IS NOT NULL"):
        new_name = normalize_company(row["name_raw"])
        if not new_name or new_name == row["name"]:
            continue
        others = [oid for oid in by_name.get(new_name, []) if oid != row["id"]]
        if others:
            collisions.append((row["id"], row["name"], new_name, others))
        else:
            name_updates.append((new_name, row["id"]))

    print(f"\ncompanies.name refresh: {len(name_updates)} safe, {len(collisions)} collisions")
    for cid, old, new, others in collisions:
        print(f"  collision: id={cid} name='{old}' -> '{new}' (already on {others})")
    conn.executemany("UPDATE companies SET name = ? WHERE id = ?", name_updates)
    conn.commit()

    # ---- Phase 5: retroactive dedup to merge any new collisions --------------
    # The cleaned company names may dedup-collide with existing clean rows
    # (e.g. "GE Healthcare" vs newly-cleaned "GE Precision Healthcare LLC").
    print("\nRunning retroactive dedup to merge any new collisions...")
    from jobcannon.engine.dedup_normalizer import run_retroactive_dedup

    merged = run_retroactive_dedup(conn)
    print(f"Merged {merged} duplicate rows.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
