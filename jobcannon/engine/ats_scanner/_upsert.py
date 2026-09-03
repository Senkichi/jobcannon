# PORTED from job_finder/web/ats_scanner/_upsert.py @ 4348fc77093fa44e7be4e29a97ded6bed7d9ced3 (private job-cannon). Ledger L-0021.
"""Company-table upsert helper for the ATS scanner.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.

NOTE: upsert_company is now re-exported from ats_company (the denylist/hygiene-
enforced implementation). Only is_company_tracked lives in this module.
"""

import logging
import sqlite3

from jobcannon.engine.dedup_normalizer import normalize_company

logger = logging.getLogger(__name__)


def is_company_tracked(conn: sqlite3.Connection, name: str) -> bool:
    """True when a company is actively tracked for ATS scanning (WP6).

    "Tracked" == a companies row exists (matched by normalized name, with a
    raw-name fallback via ``name`` OR ``name_raw``) AND ``scan_enabled = TRUE``.
    # PORT-SEAM: private 4348fc77 (#1869 WI-13) renamed this read to the
    # per-source `ats_scan_enabled` column; deferred here (see below).
    A row the user disabled shows as untracked so the Track action can
    re-enable it.
    """
    if not name:
        return False
    # PORT-SEAM: public schema (m0001_initial_schema.py) still has one
    # `scan_enabled boolean` column, not yet split into a per-source
    # `ats_scan_enabled` column (that migration is its own ledger row, not
    # yet landed) -- stays on `scan_enabled` as TRUE, not an integer literal
    # (tests/host/test_scan_dialect.py enforces the boolean-literal form for
    # Postgres compatibility), until that row lands.
    row = conn.execute(
        """SELECT 1 FROM companies
           WHERE (name = ? OR name_raw = ?) AND scan_enabled = TRUE -- # PORT-SEAM: ats_scan_enabled rename deferred, L-0021
           LIMIT 1""",
        (normalize_company(name), name),
    ).fetchone()
    return row is not None
