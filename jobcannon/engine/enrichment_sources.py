# PORTED from job_finder/web/enrichment_sources.py @ 6b00b39bb73bc9ddabf2ce20404b0521d4a587f1 (private job-cannon). Ledger L-0048.
"""URL utilities for the enrichment pipeline.

Public API:
    parse_source_urls: Parse source_urls JSON field into a list of URL strings.
    merge_apply_urls:  Read-merge-write source_urls JSON column with new ATS URLs.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_source_urls(source_urls_json: str | None) -> list:
    """Parse source_urls JSON field into a list of URL strings.

    Args:
        source_urls_json: JSON string like '["https://..."]' or None.

    Returns:
        List of URL strings. Empty list if None or unparseable.
    """
    if not source_urls_json:
        return []
    try:
        urls = json.loads(source_urls_json)
        return [u for u in urls if isinstance(u, str)]
    except (json.JSONDecodeError, TypeError):
        return []


def merge_apply_urls(conn: Any, dedup_key: str, apply_urls: list) -> None:
    """Read-merge-write source_urls JSON column with new ATS URLs from SerpAPI apply_options.

    Designed to be called AFTER persist() succeeds so both writes succeed or
    both fail together (best-effort atomicity — SQLite has no multi-statement
    rollback across these two UPDATE calls, but failure of the second is non-critical).

    Args:
        conn: Open SQLite connection.
        dedup_key: Job dedup key to update.
        apply_urls: New URL strings to merge in (deduplicated against existing list).
    """
    if not conn or not dedup_key or not apply_urls:
        return
    try:
        existing_row = conn.execute(
            "SELECT title, company FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing_row is None:
            logger.warning(
                "source_urls merge skipped: no row for dedup_key=%s "
                "(job deleted between enrich_job start and write?)",
                dedup_key,
            )
            return
        # Route through upsert_job (D-15): source_urls is a parser-owned column,
        # so its merge must pass the typed contract rather than a raw UPDATE
        # bypass. upsert_job set-unions source_urls against the existing list and
        # returns kind="touched" (no canonical change), leaving scoring,
        # pipeline_status and unresolved_reasons untouched.
        from jobcannon.engine.services import get_services  # PORT-SEAM: upsert_job routed through ScanServices (host DB write path)
        from jobcannon.engine.parsed_job import ParsedJob

        parsed = ParsedJob(
            title=existing_row["title"],
            company=existing_row["company"],
            dedup_key=dedup_key,
            source_urls=[u for u in apply_urls if u],
        )
        get_services().upsert_job(conn, parsed)  # PORT-SEAM: upsert_job routed through ScanServices
        logger.debug(
            "source_urls: merged up to %d candidate ATS URL(s) for dedup_key=%s",
            len(apply_urls),
            dedup_key,
        )
    except Exception as exc:
        logger.debug("merge_apply_urls failed for dedup_key=%s: %s", dedup_key, exc)
