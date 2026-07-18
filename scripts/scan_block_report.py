"""Per-platform scan block-rate report (operator tool feeding the OD-9
concurrency pin and OD-11 static-IP decision after the ASN load test).

Structured columns and jsonb keys only — no regex over error strings (house
rule: read structured, don't re-parse free text). If a signal isn't
structured yet, this reports the raw counts and leaves classification to the
operator.

Usage (from the deployed worker environment or a shell with DATABASE_URL set):
    python scripts/scan_block_report.py                # last 24h
    python scripts/scan_block_report.py --since 2       # last 2h (staging scan)
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row


def _platform_scan_stats(conn, since: datetime) -> list[dict]:
    return conn.execute(
        """
        SELECT c.ats_platform,
               COUNT(DISTINCT csl.company_id) AS companies_scanned,
               COUNT(*) AS scan_attempts,
               COUNT(*) FILTER (WHERE csl.error IS NOT NULL) AS errors
        FROM company_scan_log csl
        JOIN companies c ON c.id = csl.company_id
        WHERE csl.scanned_at >= %s
        GROUP BY c.ats_platform
        ORDER BY c.ats_platform NULLS LAST
        """,
        (since,),
    ).fetchall()


def _platform_retry_counts(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT ats_platform, COUNT(*) AS companies_in_retry
        FROM companies
        WHERE retry_after > now()
        GROUP BY ats_platform
        ORDER BY ats_platform NULLS LAST
        """
    ).fetchall()


def _platform_miss_reasons(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT ats_platform, miss_reason, COUNT(*) AS n
        FROM companies
        WHERE miss_reason IS NOT NULL
        GROUP BY ats_platform, miss_reason
        ORDER BY ats_platform NULLS LAST, n DESC
        """
    ).fetchall()


def _health_log_stats(conn, since: datetime) -> list[dict]:
    return conn.execute(
        """
        SELECT payload->>'source' AS source, payload->>'surface' AS surface, COUNT(*) AS n
        FROM scan_health_log
        WHERE recorded_at >= %s
        GROUP BY payload->>'source', payload->>'surface'
        ORDER BY n DESC
        """,
        (since,),
    ).fetchall()


def print_report(conn, *, since_hours: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    print(f"=== scan block-rate report (last {since_hours}h) ===\n")

    print("-- per-platform scan attempts / errors --")
    for row in _platform_scan_stats(conn, since):
        attempts = row["scan_attempts"]
        share = (row["errors"] / attempts) if attempts else 0.0
        print(
            f"{row['ats_platform'] or '(unprobed)':20s} "
            f"scanned={row['companies_scanned']:>4} attempts={attempts:>4} "
            f"errors={row['errors']:>4} ({share:.1%})"
        )

    print("\n-- companies currently in retry_after > now() --")
    for row in _platform_retry_counts(conn):
        print(f"{row['ats_platform'] or '(unprobed)':20s} in_retry={row['companies_in_retry']}")

    print("\n-- miss_reason counts --")
    for row in _platform_miss_reasons(conn):
        print(f"{row['ats_platform'] or '(unprobed)':20s} {row['miss_reason']:30s} n={row['n']}")

    print(f"\n-- scan_health_log by source/surface (last {since_hours}h) --")
    for row in _health_log_stats(conn, since):
        print(f"source={row['source']!s:20s} surface={row['surface']!s:20s} n={row['n']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", type=int, default=24, metavar="HOURS", help="lookback window in hours"
    )
    args = parser.parse_args()

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        print_report(conn, since_hours=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
