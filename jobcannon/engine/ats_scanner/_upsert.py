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
    raw-name fallback mirroring the ``_high_score_history_clause`` precedent)
    AND ``scan_enabled = TRUE``. A row the user disabled shows as untracked so
    the Track action can re-enable it.
    """
    if not name:
        return False
    row = conn.execute(
        """SELECT 1 FROM companies
           WHERE (name = ? OR name_raw = ?) AND scan_enabled = TRUE
           LIMIT 1""",
        (normalize_company(name), name),
    ).fetchone()
    return row is not None
