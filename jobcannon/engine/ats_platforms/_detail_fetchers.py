"""Per-job description detail fetchers for Workday + SmartRecruiters.

These were kept at the public ``ats_platforms`` namespace pre-H3 so that
``tests/test_workday_scanner.py`` and ``tests/test_smartrecruiters_scanner.py``
could import them directly. The H3 package promotion (2026-05-28) moves
the implementations here; the package ``__init__`` re-exports both names
so the test-facing import paths stay unchanged.

All HTTP calls use a shared ``requests.Session`` with connection pooling
(via ``_http_session.get_session()``) to avoid repeated TCP+TLS handshakes.
Unlike the ``_registry``-delegating platforms, these two fetchers import
``get_session`` directly into this module's own namespace, so tests must
patch ``jobcannon.engine.ats_platforms._detail_fetchers.get_session`` — not
``_http_session.get_session`` on the origin module (which has no effect once
this module already holds its own reference to the original function) and
not ``_registry.get_session`` (these functions don't route through
``_registry``).
"""

from __future__ import annotations

import logging

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.description_formatter import strip_html_to_text

logger = logging.getLogger(__name__)


def _fetch_workday_description(subdomain: str, tenant: str, board: str, external_path: str) -> str:
    """Fetch the full job description via Workday CXS detail endpoint.

    Workday's list endpoint returns only titles and metadata; the full HTML
    description lives at a separate per-job URL. Returns empty string on any
    failure (no exceptions leak to the caller) so one broken job doesn't kill
    a whole scan.

    Args:
        subdomain: Workday subdomain (e.g. 'walmart.wd5').
        tenant: Derived tenant (prefix before '.wd').
        board: Job board name (second half of slug).
        external_path: Posting path from the list response (e.g. '/job/Analyst_R-123').

    Returns:
        Plain-text job description (HTML stripped), or "" if fetch failed.
    """
    # external_path begins with "/job/..." — no static "/job/" prefix here.
    detail_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}{external_path}"
    try:
        resp = get_session().get(
            detail_url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
    except Exception as exc:
        logger.debug("scan_workday detail fetch failed for %s: %s", external_path, exc)
        return ""

    # Common shape: {"jobPostingInfo": {"jobDescription": "<html>..."}}
    info = data.get("jobPostingInfo") or {}
    html = info.get("jobDescription") or ""
    if not html:
        return ""

    return strip_html_to_text(html) if "<" in html else html


def _fetch_smartrecruiters_description(slug: str, posting_id: str) -> str:
    """Fetch the full job description via SmartRecruiters Posting detail API.

    The posting detail response has `jobAd.sections.*.text` fields; we
    concatenate the main job description and qualifications sections.
    Returns empty string on any failure so one broken job doesn't kill the scan.

    Args:
        slug: SmartRecruiters company identifier.
        posting_id: Posting UUID from the list response.

    Returns:
        Plain-text job description (HTML stripped), or "" on failure.
    """
    detail_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
    try:
        resp = get_session().get(
            detail_url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
    except Exception as exc:
        logger.debug("scan_smartrecruiters detail fetch failed for %s: %s", posting_id, exc)
        return ""

    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts: list[str] = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        text = section.get("text") or ""
        if text:
            parts.append(text)

    combined = "\n\n".join(parts)
    if not combined:
        return ""

    return strip_html_to_text(combined) if "<" in combined else combined
