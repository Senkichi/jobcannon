# PORTED from job_finder/web/enrichment_tiers.py @ 0a4c33c5af7cd4055e539672158cb301b7bdc407 (private job-cannon). Ledger L-0178.
"""Free-tier ATS-board enrichment: DB-cache-or-live board query + title match.

Split out of the private ``enrichment_tiers.py`` (design note PR-4). Binds
to ``ScanServices.query_ats_api``.

# PORT-SEAM: the private module's sibling ``scrape_careers`` (the HTML
# careers-page-scrape sub-tier, binds to ``ScanServices.scrape_careers_tier``)
# is NOT ported in this PR. It imports ``find_careers_url`` /
# ``scrape_careers_page`` from ``careers_crawler.py`` (L-0167), which lives
# in this same design-note unit's PR-A (#369) and has not yet merged to
# main — porting it here would violate the boundary guard (a module absent
# from this branch). Deferred to a follow-up once #369 lands; the
# ``scrape_careers_tier`` ScanServices field stays unbound (None) until then.
"""

from __future__ import annotations

import logging
from typing import Any

from jobcannon.engine.ats_platforms import SCANNERS_BY_NAME, run_platform_scan
from jobcannon.engine.ats_platforms._registry import BoardGoneError
from jobcannon.engine.ats_platforms._title_match import _title_matches
from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS
from jobcannon.engine.direct_link import resolve_primary_posting
from jobcannon.engine.json_utils import safe_json_load
from jobcannon.engine.location_canonical import from_list as _locations_from_list
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)


def _scan_cache_is_fresh(cached_at: str | None, ttl_s: int) -> bool:
    """Return True if cached_at is a naive UTC ISO timestamp within ttl_s.

    A TTL of 0 disables the cache (always returns False). An unparseable
    cached_at is treated as stale.
    """
    from datetime import UTC, datetime

    if not cached_at or ttl_s <= 0:
        return False
    try:
        parsed = datetime.fromisoformat(cached_at)
        now = datetime.now(UTC).replace(tzinfo=None)
        return (now - parsed).total_seconds() <= ttl_s
    except (ValueError, TypeError):
        return False


def _rebuild_locations_structured(postings: list[dict]) -> list[dict]:
    """Reconstruct ``JobLocation`` instances for postings from the JSON cache.

    The ATS scan cache round-trips ``JobLocation`` dataclasses through JSON via
    ``asdict()``. Reading that cache back yields plain dicts, which crash
    downstream consumers that call ``asdict()`` expecting real dataclass
    instances. This normalizes in place before the posting list is used.

    Idempotent for live-scan postings that already carry ``JobLocation`` objects.
    """
    for posting in postings:
        raw = posting.get("locations_structured")
        if isinstance(raw, list):
            posting["locations_structured"] = _locations_from_list(raw)
    return postings


def _build_ats_result(
    postings: list[dict],
    title: str,
    location: str,
    target_titles: list[str],
    exclusions: list[str],
    platform: str | None = None,
) -> dict:
    """Run the title gate + resolve_primary_posting on a candidate posting list.

    Shared by the live run_platform_scan path and the cached scan path.
    When ``platform`` is provided it is stamped on the matched primary posting
    so ``merge_primary_posting_fields`` can create/update the ``postings``
    sub-entity descriptor.
    """
    matched = [
        posting
        for posting in postings
        if _title_matches(posting.get("title", ""), target_titles, exclusions)
    ]
    if not matched:
        return {}

    resolved = resolve_primary_posting(matched, title, location)
    if resolved is None:
        return {}
    posting, url, confidence = resolved

    if posting is not None and platform:
        posting["ats_platform"] = platform

    svc = get_services()  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
    result: dict = {"direct_url": url, "direct_url_confidence": confidence}
    if posting is not None:
        if posting.get("description"):
            result["jd_full"] = posting["description"][: svc.jd_storage_max_chars]
        if posting.get("salary_min"):
            result["salary_min"] = posting["salary_min"]
        if posting.get("salary_max"):
            result["salary_max"] = posting["salary_max"]
        result["_primary_posting"] = posting

    return result


def query_ats_api(job_row: dict, conn: Any, config: dict) -> dict:
    """Query ATS API (Lever/Greenhouse/Ashby) for job data if company has a slug.

    Looks up the company record from the DB. If ats_probe_status='hit', calls
    the appropriate ATS scan function with a loose title match derived from
    significant words in the job title.

    If the company has a fresh DB-backed scan cache (companies.last_scan_cached_at
    within ats.enrichment_board_cache_ttl_s), the cached posting list is reused
    and the live run_platform_scan call is skipped.

    Args:
        job_row: Job row dict with company_id field.
        conn: Open DB connection.
        config: Application config dict.

    Returns:
        Dict with direct_url + direct_url_confidence when any posting links,
        plus jd_full / salary_min / salary_max / _primary_posting ONLY on a
        strict (unambiguous) title match. Empty if not found.
    """
    try:
        company_id = job_row.get("company_id")
        if not company_id:
            return {}

        company_row = conn.execute(
            "SELECT ats_platform, ats_slug, ats_probe_status, "
            "last_scan_cached_at, last_scan_postings_json "
            "FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

        if not company_row:
            return {}

        company = dict(company_row)
        if company.get("ats_probe_status") != "hit":
            return {}

        platform = company.get("ats_platform")
        slug = company.get("ats_slug")
        if not platform or not slug:
            return {}

        title = job_row.get("title", "")
        target_titles = [w for w in title.split() if len(w) > 3]
        exclusions = config.get("scoring", {}).get("exclusions", [])

        ttl_s = int(config.get("ats", {}).get("enrichment_board_cache_ttl_s", 86400))

        postings: list[dict] | None = None
        if _scan_cache_is_fresh(company.get("last_scan_cached_at"), ttl_s):
            cached = safe_json_load(company.get("last_scan_postings_json"), default=None)
            if isinstance(cached, list) and cached:
                logger.debug(
                    "ATS enrichment cache hit for company_id=%s (platform=%s)",
                    company_id,
                    platform,
                )
                postings = cached

        if postings is None:
            scanner = SCANNERS_BY_NAME.get(platform)
            if scanner is None or platform in NON_SCANNABLE_PLATFORMS:
                return {}
            try:
                postings, _ = run_platform_scan(scanner, slug, target_titles, exclusions)
            except BoardGoneError:
                # The company's board 404/410'd — no data to enrich from. Degrade to
                # "no enrichment" (the pre-existing behavior for an empty board); the
                # stale-hit demotion is the scan path's job, not enrichment's.
                return {}

        if not postings:
            return {}

        postings = _rebuild_locations_structured(postings)
        return _build_ats_result(
            postings,
            title,
            job_row.get("location") or "",
            target_titles,
            exclusions,
            platform=platform,
        )

    except Exception as e:
        logger.warning("ATS API query failed: %s", e)
        return {}
