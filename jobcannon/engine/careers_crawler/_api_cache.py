# PORTED from job_finder/web/careers_crawler/_api_cache.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0443 (umbrella -- no ledger row of its own; imported at module scope by careers_crawler/__init__.py, so the package is unlandable without it -- see PR body).
"""Discovered API-endpoint cache for the careers crawler.

Three small functions that manage the per-company `careers_api_endpoint`
column on the `companies` table. The orchestrator caches an endpoint
when the Playwright tier intercepts an XHR that returns matchable
postings; subsequent runs short-circuit to the API directly.

All functions are best-effort — exceptions are logged at debug level
and swallowed so a transient DB hiccup never aborts a crawl.
"""

from __future__ import annotations

import logging

import requests

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.services import get_services  # PORT-SEAM: connection_factory (L-0443)

logger = logging.getLogger(__name__)


def _try_cached_api(
    api_endpoint: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict] | None:
    """Try fetching jobs from a previously discovered API endpoint.

    Returns:
        list[dict] — jobs found (may be empty but endpoint is working)
        None — endpoint is broken/unreachable (caller should clear cache)
    """
    from jobcannon.engine.careers_page_interactions import parse_api_response

    try:
        resp = fetch_with_deadline(
            api_endpoint, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS
        )
        if resp.status_code >= 400:
            logger.debug(
                "Cached API endpoint returned %d: %s",
                resp.status_code,
                api_endpoint,
            )
            return None

        data = resp.json()
        return parse_api_response(data, target_titles, exclusions)

    except Exception as e:
        logger.debug("Cached API endpoint failed: %s — %s", api_endpoint, e)
        return None


def _cache_api_endpoint(
    # PORT-SEAM: db_path param dropped -- svc.connection_factory() is zero-arg (L-0443)
    company_id: int,
    api_endpoint: str,
) -> None:
    """Store a discovered API endpoint for future fast-path access."""
    try:
        svc = get_services()  # PORT-SEAM: seam (L-0443)
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0443)
            conn.execute(
                "UPDATE companies SET careers_api_endpoint = ? WHERE id = ?",
                (api_endpoint, company_id),
            )
            conn.commit()
        logger.info(
            "Cached API endpoint for company %d: %s",
            company_id,
            api_endpoint,
        )
    except Exception as e:
        logger.debug("Failed to cache API endpoint: %s", e)


def _clear_api_cache(company_id: int) -> None:
    # PORT-SEAM: db_path param dropped -- svc.connection_factory() is zero-arg (L-0443)
    """Clear a stale cached API endpoint."""
    try:
        svc = get_services()  # PORT-SEAM: seam (L-0443)
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0443)
            conn.execute(
                "UPDATE companies SET careers_api_endpoint = NULL WHERE id = ?",
                (company_id,),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Failed to clear API cache: %s", e)
