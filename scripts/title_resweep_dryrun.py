"""Read-only DRY-RUN of the title-hygiene re-sweep (I-16/I-17).

Reports exactly what ``_run_title_resweep_if_stale`` WOULD do against a database
— how many titles it would repair, quarantine, and declassify, with samples —
WITHOUT mutating anything. Run this before letting the version bump heal the
corpus on the next app startup, to confirm the blast radius is what you expect
(the adversarial review's mandated safety gate, since the re-sweep is the first
code that rewrites a stored title).

Usage (PowerShell):
    $env:PYTHONUTF8=1; python scripts/title_resweep_dryrun.py [path/to/jobs.db]

Defaults to JOB_CANNON_USER_DATA_DIR/jobs.db, then ./jobs.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter

# Import the SAME functions the live re-sweep uses, so the dry-run can never
# drift from the real heal.
from jobcannon.engine.normalizers import normalize_title
from jobcannon.engine.careers_crawler._title_contract import (
    TITLE_HYGIENE_VERSION,
    TITLE_REASON_CODES,
    title_contract_violation,
    title_jd_mismatch,
)
from jobcannon.engine.careers_crawler._title_filters import _strip_trailing_card_junk


def _resolve_db_path(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1]
    root = os.environ.get("JOB_CANNON_USER_DATA_DIR")
    if root and os.path.exists(os.path.join(root, "jobs.db")):
        return os.path.join(root, "jobs.db")
    return "jobs.db"


def main(argv: list[str]) -> int:
    db_path = _resolve_db_path(argv)
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        wm = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'title_hygiene_version'"
        ).fetchone()
        stored = wm[0] if wm else "(unseeded — m110 not yet applied)"
    except sqlite3.OperationalError:
        stored = "(schema_meta absent)"

    rows = conn.execute(
        "SELECT title, jd_full, unresolved_reasons, classification FROM jobs"
    ).fetchall()

    total = len(rows)
    rewrite = quarantine = declassify = recleared = jd_informational = 0
    reason_counts: Counter[str] = Counter()
    rewrite_samples: list[tuple[str, str]] = []
    quarantine_samples: list[tuple[str, str]] = []

    for r in rows:
        title = r["title"]
        if title is None:
            continue
        new_title = _strip_trailing_card_junk(title)

        try:
            old_reasons = json.loads(r["unresolved_reasons"]) if r["unresolved_reasons"] else []
            if not isinstance(old_reasons, list):
                old_reasons = []
        except (TypeError, ValueError):
            old_reasons = []

        # The re-sweep applies ONLY the shape/non-posting contract (title_jd_mismatch
        # is deferred — it flags garbage JDs, not titles). Mirror that here.
        new_reasons = [x for x in old_reasons if x not in TITLE_REASON_CODES]
        shape_reason = title_contract_violation(new_title)
        if shape_reason is not None:
            new_reasons.append(shape_reason)

        # Informational only (NOT applied by the re-sweep): the garbage-JD cohort.
        if title_jd_mismatch(new_title, r["jd_full"]):
            jd_informational += 1

        title_changed = new_title != title
        had_title_reason = any(x in TITLE_REASON_CODES for x in old_reasons)
        has_title_reason = any(x in TITLE_REASON_CODES for x in new_reasons)
        semantic_change = title_changed and normalize_title(new_title) != normalize_title(title)

        if title_changed:
            rewrite += 1
            if semantic_change and len(rewrite_samples) < 15:
                rewrite_samples.append((title, new_title))
        if not had_title_reason and has_title_reason:
            quarantine += 1
            new_code = next(x for x in new_reasons if x in TITLE_REASON_CODES)
            reason_counts[new_code] += 1
            if len(quarantine_samples) < 15:
                quarantine_samples.append((new_title, new_code))
        elif had_title_reason and not has_title_reason:
            recleared += 1
        if ((has_title_reason and not had_title_reason) or semantic_change) and r[
            "classification"
        ] is not None:
            declassify += 1

    conn.close()

    print(f"DB: {db_path}")
    print(
        f"stored title_hygiene_version = {stored} | live TITLE_HYGIENE_VERSION = {TITLE_HYGIENE_VERSION}"
    )
    print(f"rows scanned: {total}")
    print("-" * 70)
    print(f"would REPAIR (title rewritten): {rewrite}")
    print(f"would QUARANTINE (newly flagged): {quarantine}")
    for code, n in reason_counts.most_common():
        print(f"    {n:5d}  {code}")
    print(f"would RE-CLEAR (flag removed):   {recleared}")
    print(f"would DECLASSIFY (drop from board / re-score): {declassify}")
    print(
        f"[informational only, NOT applied] title<->JD zero-overlap (garbage-JD cohort): "
        f"{jd_informational}"
    )
    print("-" * 70)
    print("REPAIR samples — SEMANTIC changes only (old -> new):")
    for old, new in rewrite_samples:
        print(f"  {old!r}\n    -> {new!r}")
    print("\nQUARANTINE samples (cleaned title | reason):")
    for t, code in quarantine_samples:
        print(f"  [{code}] {t!r}")
    print("\n(DRY RUN — no rows were modified.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
