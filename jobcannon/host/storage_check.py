"""Storage-percentage alert, code-owned so it is CI-testable and not
dependent on remembering a dashboard setting. Render's managed-Postgres email
notifications are the belt; this periodic check is the suspenders — it logs
at ERROR and writes a scan_health_log row through the sanctioned recorder,
so the operator sees it in the same place as every other health signal."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ALERT_THRESHOLD = 0.8


def check_db_storage(conn: Any, *, limit_mb: int) -> dict:
    raw = conn.raw if hasattr(conn, "raw") else conn
    used_bytes = raw.execute(
        "SELECT pg_database_size(current_database()) AS used_bytes"
    ).fetchone()["used_bytes"]
    used_pct = used_bytes / (limit_mb * 1024 * 1024)
    alert = used_pct >= ALERT_THRESHOLD
    if alert:
        logger.error(
            "database storage at %.1f%% of the %sMB tier — plan the tier upgrade",
            used_pct * 100,
            limit_mb,
        )
    return {
        "used_bytes": used_bytes,
        "limit_mb": limit_mb,
        "used_pct": round(used_pct, 4),
        "alert": alert,
    }
