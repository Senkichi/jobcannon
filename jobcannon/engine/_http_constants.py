"""Shared HTTP constants for outbound requests.

These constants are imported by `enrichment_tiers.py`, `careers_crawler.py`,
and `careers_page_interactions.py`. Centralizing them here removes the
back-edge import (`careers_crawler` → `enrichment_tiers`) called out in
`.planning/portfolio-cleanup/module-shapes.md` "Cross-Cutting Notes" — that
back-edge was the precondition that gated the 7e split per S0 hand-off, and
is the reason MI-11's "7a–7e are interchangeable" claim was conditional on
this extraction landing first.
"""

# User-Agent + accept headers for crawler/scraper traffic. Identifies the
# project so site operators can attribute traffic; matches the original UA
# string previously defined inline in enrichment_tiers.py to preserve any
# request-log signatures.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; JobFinder/1.0; +https://github.com/job-finder)")
}

# Default timeout (seconds) for external HTTP API calls. Short enough to keep
# the per-request budget bounded in batch crawls, long enough to absorb a
# typical TLS + first-byte round trip on slow careers pages.
_TIMEOUT = 10
