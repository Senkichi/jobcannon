# PORTED from job_finder/parsers/__init__.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Email parsers for different job board alert formats."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from jobcannon.engine.email_parsers._positional_fallback import has_job_urls, positional_fallback
from jobcannon.engine.email_parsers.glassdoor_parser import parse_glassdoor_alert
from jobcannon.engine.email_parsers.greenhouse_parser import parse_greenhouse_alert
from jobcannon.engine.email_parsers.indeed_parser import parse_indeed_alert
from jobcannon.engine.email_parsers.indeed_parser import parse_indeed_match_alert
from jobcannon.engine.email_parsers.jobright_parser import parse_jobright_alert
from jobcannon.engine.email_parsers.linkedin_parser import parse_linkedin_alert
from jobcannon.engine.email_parsers.monster_parser import parse_monster_alert
from jobcannon.engine.email_parsers.trueup_parser import parse_trueup_alert
from jobcannon.engine.email_parsers.ziprecruiter_parser import parse_ziprecruiter_alert

logger = logging.getLogger(__name__)


def extract_with_fallback(
    primary_fn: Callable,
    body: str,
    email_date: datetime | None,
) -> list:
    """Run the primary parser; fall back to positional extraction only when needed.

    The fallback fires **only** if all three conditions hold:
    1. The primary parser returned an empty list.
    2. The body contains at least one recognised job-board / ATS URL.
    3. The body is not empty.

    This ensures genuine meta/empty emails are never handed to the fallback,
    and that a working primary parse is never overridden.

    Args:
        primary_fn: The parser callable, e.g. ``parse_linkedin_alert``.
        body: Raw email body text.
        email_date: When the email was sent.

    Returns:
        Jobs from the primary parser, or (if empty + job URL present) jobs
        from the positional fallback, or ``[]``.
    """
    jobs = extract_primary(primary_fn, body, email_date)
    if jobs:
        return jobs
    if body and has_job_urls(body):
        fallback_jobs = positional_fallback(body, email_date)
        if fallback_jobs:
            logger.debug(
                "extract_with_fallback: primary empty; positional fallback found %d job(s)",
                len(fallback_jobs),
            )
        return fallback_jobs
    return []


def extract_primary(
    primary_fn: Callable,
    body: str,
    email_date: datetime | None,
) -> list:
    """Run ONLY the primary parser step of ``extract_with_fallback``.

    The Phase D shadow guard compares an active override against this — not
    against the positional fallback, whose loose extraction could "win" a
    count comparison with garbage and get a good override deleted. The
    primary parser is the meaningful health signal.

    Args:
        primary_fn: The parser callable, e.g. ``parse_linkedin_alert``.
        body: Raw email body text.
        email_date: When the email was sent.

    Returns:
        Jobs from the primary parser, or ``[]``.
    """
    return primary_fn(body, email_date) or []


__all__ = [
    "extract_primary",
    "extract_with_fallback",
    "has_job_urls",
    "parse_glassdoor_alert",
    "parse_greenhouse_alert",
    "parse_indeed_alert",
    "parse_indeed_match_alert",
    "parse_jobright_alert",
    "parse_linkedin_alert",
    "parse_monster_alert",
    "parse_trueup_alert",
    "parse_ziprecruiter_alert",
]
