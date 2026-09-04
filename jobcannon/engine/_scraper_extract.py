# PORTED from job_finder/web/careers_scraper.py @ e218953fefd532d071482e3ff859610a46dd179d (private job-cannon). Ledger L-0167.
"""Low-tier (quick-model) fallback helpers for the careers-page scraper.

Split out of ``careers_scraper.py`` (design note PR-4) to isolate the
call_model-touching code from the pure HTML-parsing / host-classification
logic in that module.

``call_model`` is threaded as an injected, OPTIONAL keyword-only callable
(job_scorer.score_job precedent for the injection shape itself), but kept
optional here to preserve this module's existing conn/config optionality:
the low-tier fallback in the caller only fires when conn, config, AND
call_model are all supplied -- callers that want heuristic-only, zero-cost
behavior simply omit it.

# PORT-SEAM: the private ``ProviderCascadeExhaustedError`` retry-via-CLI
# fallback (call_model, then call_claude on cascade exhaustion) is DROPPED.
# The public call_model dispatcher's own cascade already includes the CLI
# tier (Ollama -> Gemini -> Claude Code CLI -> Anthropic, per CLAUDE.md), so
# the private module's manual two-step try/except collapses to a single
# call_model invocation; any remaining failure is caught by the existing
# outer ``except Exception`` and treated as a fallback miss (returns
# None / []). This also drops the legacy ``from job_finder.web.claude_client
# import call_claude`` import per the design note's explicit instruction.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable
from urllib.parse import urljoin

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.platform_extractor import extract_clean_jd
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)

_LOW_TIER_HTML_CHARS = 3000  # Truncate HTML sent to low tier (~1000 tokens)

_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]

# Structured output schemas for the two quick-tier call sites below. Both
# the CLI (Anthropic) and Ollama return the same dict shape when a schema is
# supplied -- without it, Ollama's forced "format":"json" yields arbitrary
# keys while the CLI wraps freeform text in {"text": ...}, which would
# silently produce empty results once a cascade routes the call through
# Ollama.
_CAREERS_URL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Absolute URL to the careers/jobs page, or the word 'none' if not found",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

_CAREERS_JOBS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


def _find_careers_url_with_low_tier(
    homepage_url: str,
    homepage_html: str,
    conn: sqlite3.Connection,
    config: dict,
    *,
    call_model: Callable[..., Any],
) -> str | None:
    """Use low-tier model to identify careers page URL from homepage HTML.

    Only called when heuristic link-finding fails. Truncates HTML to
    _LOW_TIER_HTML_CHARS (~1000 tokens) to minimize cost.

    Args:
        homepage_url: The homepage URL (for resolving relative URLs).
        homepage_html: Raw HTML of the homepage.
        conn: SQLite connection for cost recording.
        config: Application config dict.
        call_model: Injected model-dispatch callable, matching the private
            repo's ``model_provider.call_model`` signature (tier, system,
            messages, conn, config, output_schema, job_id, purpose,
            max_tokens). # PORT-SEAM: the engine has no provider of its own;
            the host supplies this.

    Returns:
        Absolute URL to the careers page, or None if not found.
    """
    truncated_html = homepage_html[:_LOW_TIER_HTML_CHARS]

    system = (
        "You identify careers/jobs page URLs from company website HTML. "
        "Return the absolute URL in the 'url' field, or the string 'none' in "
        "the 'url' field when no careers page is found."
    )
    messages = [
        {
            "role": "user",
            "content": f"Given this company homepage HTML from {homepage_url}, identify the URL for their careers or jobs page.\n\nHTML:\n{truncated_html}",
        }
    ]

    try:
        model_result = call_model(
            tier="quick",
            system=system,
            messages=messages,
            conn=conn,
            config=config,
            output_schema=_CAREERS_URL_SCHEMA,
            job_id=None,
            purpose="find_careers_url",
            max_tokens=256,
        )
        result = model_result.data
    except Exception as e:
        logger.debug("Low-tier careers URL fallback failed for '%s': %s", homepage_url, e)
        return None

    url_text = (result.get("url", "") if isinstance(result, dict) else "").strip()
    if not url_text or url_text.lower() == "none":
        return None

    # Resolve relative URL
    if url_text.startswith("/"):
        url_text = urljoin(homepage_url, url_text)

    # Basic validation: must start with http
    if url_text.startswith("http"):
        logger.debug("quick-tier found careers URL for '%s': %s", homepage_url, url_text)
        return url_text

    return None


def _fetch_job_description(url: str) -> str:
    """Fetch a job page and extract cleaned description text.

    Delegates structure-aware extraction to ``platform_extractor.extract_clean_jd``
    (platform-scoped container -> trafilatura markdown + block dedup + page-chrome
    strip), checks for auth-wall signatures, and caps output at
    ``svc.jd_storage_max_chars``. Returns empty string on any failure (never
    None).

    Args:
        url: Job page URL to fetch.

    Returns:
        Cleaned description text, or empty string on failure.
    """
    try:
        resp = fetch_with_deadline(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        # Route through the single chokepoint so any platform-scoped pages and
        # trailing page chrome are handled identically to the other fetch tiers.
        text = extract_clean_jd(url, resp.text) or ""
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth-wall detected for job page '%s'", url)
            return ""
        if not text.strip():
            return ""
        svc = get_services()  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
        return text[: svc.jd_storage_max_chars]
    except Exception as e:
        logger.debug("Failed to fetch job description from '%s': %s", url, e)
        return ""


def _extract_jobs_with_low_tier(
    careers_url: str,
    careers_html: str,
    target_titles: list[str],
    exclusions: list[str],
    conn: sqlite3.Connection,
    config: dict,
    *,
    call_model: Callable[..., Any],
) -> list[dict]:
    """Extract job listings from unstructured careers page HTML using low-tier model.

    Called when HTML link-parsing finds 0 results. Sends truncated HTML
    to low tier for structured extraction.

    Args:
        careers_url: URL of the careers page (for resolving relative URLs).
        careers_html: Raw HTML of the careers page.
        target_titles: Target title keywords for post-extraction filtering.
        exclusions: Exclusion keywords for post-extraction filtering.
        conn: SQLite connection for cost recording.
        config: Application config dict.
        call_model: Injected model-dispatch callable (see
            ``_find_careers_url_with_low_tier``).

    Returns:
        List of dicts with title, url, description keys. May be empty.
    """
    truncated_html = careers_html[:_LOW_TIER_HTML_CHARS]

    system = (
        "You extract job listings from careers page HTML. Populate the 'jobs' "
        "array with objects containing 'title' (string, required), 'url' "
        "(string, optional), and 'location' (string, optional). If no jobs are "
        "found, return an empty 'jobs' array."
    )
    messages = [
        {
            "role": "user",
            "content": f"Extract job listings from this careers page ({careers_url}):\n\n{truncated_html}",
        }
    ]

    try:
        model_result = call_model(
            tier="quick",
            system=system,
            messages=messages,
            conn=conn,
            config=config,
            output_schema=_CAREERS_JOBS_SCHEMA,
            job_id=None,
            purpose="extract_jobs",
            max_tokens=1024,
        )
        result = model_result.data

        jobs = result.get("jobs", []) if isinstance(result, dict) else []
        if not isinstance(jobs, list):
            return []

        filtered = []
        for job in jobs:
            title = job.get("title", "")
            if not title or not _title_matches(title, target_titles, exclusions):
                continue
            url = job.get("url") or ""
            if url.startswith("/"):
                url = urljoin(careers_url, url)
            filtered.append(
                {
                    "title": title,
                    "url": url,
                    "description": "",  # No JD fetch for low-tier-extracted jobs (too costly)
                }
            )

        logger.debug(
            "_extract_jobs_with_low_tier('%s'): %d jobs extracted, %d after filter",
            careers_url,
            len(jobs),
            len(filtered),
        )
        return filtered

    except Exception as e:
        logger.debug("Low-tier job extraction failed for '%s': %s", careers_url, e)
        return []
