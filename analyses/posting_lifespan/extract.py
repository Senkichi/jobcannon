"""Extract right-censored posting-lifespan records from the source corpus.

Clock: first_seen -> last_seen (scanner observation window), NOT posted_date
(only ~34% populated, precision varies). Event = expiry_status 'expired';
censored = 'live'. 'inconclusive'/NULL are excluded from the primary estimate
and available as a censored robustness variant.
"""

from datetime import datetime, timezone
from sqlite3 import Connection

import pandas as pd

MIN_FIRST_SEEN = "2000-01-01"  # kills epoch-garbage rows (3 in live corpus)

LIFESPAN_SQL = """
SELECT c.ats_platform AS platform,
       j.first_seen,
       j.last_seen,
       j.expiry_status
FROM jobs j
JOIN companies c ON j.company_id = c.id
WHERE c.ats_platform IS NOT NULL
  AND j.first_seen > :min_first_seen
  AND j.last_seen >= j.first_seen
  AND j.expiry_status IN ({statuses})
"""

EXCLUSION_SQLS = {
    "total": "SELECT COUNT(*) FROM jobs",
    "no_company_join": (
        "SELECT COUNT(*) FROM jobs j LEFT JOIN companies c ON j.company_id = c.id "
        "WHERE c.id IS NULL"
    ),
    "null_platform": (
        "SELECT COUNT(*) FROM jobs j JOIN companies c ON j.company_id = c.id "
        "WHERE c.ats_platform IS NULL"
    ),
    "bad_first_seen": (
        f"SELECT COUNT(*) FROM jobs WHERE first_seen IS NULL OR first_seen <= '{MIN_FIRST_SEEN}'"
    ),
    "negative_window": "SELECT COUNT(*) FROM jobs WHERE last_seen < first_seen",
    "inconclusive_or_null_expiry": (
        "SELECT COUNT(*) FROM jobs "
        "WHERE expiry_status IS NULL OR expiry_status NOT IN ('live', 'expired')"
    ),
}


def _parse_utc_naive(value: str) -> datetime:
    """Parse an ISO timestamp, normalizing to naive UTC.

    The corpus convention is naive-UTC-only, but a small share of rows carry
    a UTC offset from a historical write-path bug (docs: store-UTC-render-local
    invariant). Normalize instead of raising on aware/naive subtraction.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _duration_days(first_seen: str, last_seen: str) -> float:
    delta = _parse_utc_naive(last_seen) - _parse_utc_naive(first_seen)
    return delta.total_seconds() / 86400.0


def load_lifespan_records(
    con: Connection, include_inconclusive_as_censored: bool = False
) -> pd.DataFrame:
    statuses = (
        "'live', 'expired', 'inconclusive'"
        if include_inconclusive_as_censored
        else "'live', 'expired'"
    )
    sql = LIFESPAN_SQL.format(statuses=statuses)
    rows = con.execute(sql, {"min_first_seen": MIN_FIRST_SEEN}).fetchall()
    records = [
        {
            "platform": platform,
            "duration_days": _duration_days(first_seen, last_seen),
            "observed": 1 if expiry_status == "expired" else 0,
        }
        for platform, first_seen, last_seen, expiry_status in rows
    ]
    return pd.DataFrame(records, columns=["platform", "duration_days", "observed"])


def load_exclusion_counts(con: Connection) -> dict[str, int]:
    counts = {name: con.execute(sql).fetchone()[0] for name, sql in EXCLUSION_SQLS.items()}
    counts["usable"] = len(load_lifespan_records(con))
    return counts
