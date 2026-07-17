"""Google Careers platform scanner — non-scannable STUB. Always returns [].

Google has no public unauthenticated JSON board. The legacy
``careers.google.com/api/v3/search/`` endpoint returns 404; the live careers
path is a JavaScript SPA whose jobs load via the obfuscated ``batchexecute`` RPC
(POST, anti-JSON ``)]}'`` prefix, positional arrays whose indices drift per
frontend build, session-bound, bot-gated). Reaching it requires a headless
browser this project deliberately avoids.

So ``google`` is registered as a non-scannable platform (see
``NON_SCANNABLE_PLATFORMS`` in ``__init__.py``) — the stub keeps the platform in
``SCANNERS_BY_NAME`` so a company can be classified ``ats_platform='google'`` and
surfaced with a "no public API" badge instead of a misleading "0 jobs". Google
roles are sourced via the aggregators (SerpApi ``google_jobs`` / DataForSEO).

NOTE: the DeepMind Greenhouse board is a *different* employer — do not relabel
it "Google".
"""

from __future__ import annotations

import logging

from jobcannon.engine.ats_platforms._registry import PlatformScanner

logger = logging.getLogger(__name__)


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """No public board — always empty. See module docstring."""
    logger.debug("scan_google('%s'): no public API (non-scannable stub)", slug)
    return []


SCANNER = PlatformScanner(
    name="google",
    company_source="Google",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=lambda posting, slug: None,
)
