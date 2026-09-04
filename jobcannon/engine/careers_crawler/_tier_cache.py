# PORTED from job_finder/web/careers_crawler/_tier_cache.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0443 (umbrella -- no ledger row of its own; imported at module scope by careers_crawler/__init__.py, so the package is unlandable without it -- see PR body).
"""Tier cache replay for the careers crawler.

The orchestrator records which extraction tier last produced jobs for a
given company on the `careers_crawl_tier` column. On the next run,
`_try_cached_tier` short-circuits the full escalation chain by trying
that tier first; on success the orchestrator skips the rest of the
escalation, on failure (empty result) it falls through to the full
chain.

This module sits at the top of the tier dependency graph — it composes
the api-cache, Playwright, and AI-nav tiers — and is therefore the
last leaf to extract before the orchestrator itself.
"""

from __future__ import annotations

import logging

from jobcannon.engine.careers_crawler._api_cache import _cache_api_endpoint, _try_cached_api
from jobcannon.engine.careers_crawler._playwright_tier import (
    # PORT-SEAM: _JS_SETTLE_MS/_wait_for_js_settle dropped -- only used by the
    # deleted ai-nav replay branch (ai_career_navigator DIES, L-0133)
    _try_playwright_active,
    _try_playwright_extract,
)

logger = logging.getLogger(__name__)


def _try_cached_tier(
    cached_tier: str,
    browser,
    company: dict,
    careers_url: str,
    api_endpoint: str | None,
    target_titles: list[str],
    title_exclusions: list[str],
    search_keywords: list[str],
    config: dict,
    # PORT-SEAM: db_path param dropped -- _cache_api_endpoint is zero-db_path,
    # svc.connection_factory() is self-seamed (L-0443)
    company_id: int,
    local_summary: dict,
) -> list[dict]:
    """Attempt extraction using the previously successful tier.

    Returns a list of job dicts on success, empty list on failure (triggering
    full escalation chain in the caller).
    """
    from jobcannon.engine.careers_page_interactions import probe_url_params

    try:
        if cached_tier == "api_cached" and api_endpoint:
            api_jobs = _try_cached_api(api_endpoint, target_titles, title_exclusions)
            if api_jobs is not None:
                local_summary["api_cached"] += 1
                return api_jobs
        elif cached_tier == "url_param" and search_keywords:
            param_jobs = probe_url_params(
                careers_url,
                search_keywords,
                target_titles,
                title_exclusions,
            )
            if param_jobs:
                local_summary["url_param_hits"] += 1
                return param_jobs
        elif cached_tier == "playwright":
            crawl_cfg = config.get("careers_crawl", {})
            interactive_enabled = crawl_cfg.get("interactive_enabled", True)
            if interactive_enabled:
                pw_jobs, discovered_api = _try_playwright_active(
                    # PORT-SEAM: db_path= kwarg dropped -- _try_playwright_active
                    # no longer takes it (L-0469 self-seamed record_careers_capture)
                    browser,
                    careers_url,
                    target_titles,
                    title_exclusions,
                    search_keywords,
                    config,
                )
                if pw_jobs:
                    local_summary["playwright_rendered"] += 1
                    if discovered_api:
                        _cache_api_endpoint(company_id, discovered_api)
                    return pw_jobs
            else:
                pw_jobs = _try_playwright_extract(
                    # PORT-SEAM: db_path= kwarg dropped -- see above
                    browser,
                    careers_url,
                    target_titles,
                    title_exclusions,
                )
                if pw_jobs:
                    local_summary["playwright_rendered"] += 1
                    return pw_jobs
        # PORT-SEAM: ai_replay/ai_navigate branch DELETED -- ai_career_navigator
        # DIES (ledger L-0133); same ruling that removes the ai-nav tier from
        # __init__.py (design note deletion set).
    except Exception:
        pass  # Cache miss — fall through to full escalation

    return []
