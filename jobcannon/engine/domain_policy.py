# PORTED from job_finder/web/domain_policy.py @ 263634cd9339885990d2510e476ad6679b675cf7 (private job-cannon). Ledger L-0177.
"""Centralized domain policy for the job enrichment pipeline.

Defines which domains are blocked (aggregator sites that gate content behind
logins or Cloudflare walls) and which are prioritized (ATS platforms and job
boards with reliable full JD content).

Design constraints:
- Zero imports from any jobcannon.engine.* module (only Python stdlib permitted).
  This prevents circular import risk: data_enricher -> domain_policy <- enrichment_tiers
  <- data_enricher is safe only when domain_policy has no back-edges into the graph.
- All data is defined as module-level constants.
- PRIORITY_DOMAINS is a list[str], NOT a frozenset — the ordering is load-bearing
  for domain_priority() which uses enumerate() to assign rank scores.
"""

__all__ = [
    "BLOCKED_DOMAINS",
    "PRIORITY_DOMAINS",
    "domain_priority",
    "is_aggregator_or_job_board",
    "is_blocked_domain",
]

# ---------------------------------------------------------------------------
# Blocked domains: aggregator/job-board sites that gate content behind login
# walls, Cloudflare challenges, or other scraping barriers. Fetching these
# in the free-tier direct-fetch pipeline is wasteful (always 403/challenge).
#
# MEMBERSHIP CONSTRAINT (from spec):
# - glassdoor.com and glassdoor.co.uk: Cloudflare 403 on all direct endpoints
# - indeed.com: often shows interstitial / login wall
# - ziprecruiter.com: rate-limited, paywalled content
# - dice.com: gated postings
# - linkedin.com must NOT be added here — fetch_linkedin_jd() handles it via
#   the specialized guest-page extractor path in data_enricher's free tier.
#   Adding linkedin.com would cause is_blocked_domain() to skip all LinkedIn
#   URL fetching, breaking the free-tier LinkedIn JD extraction.
# ---------------------------------------------------------------------------

BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        # Aggregators that gate content behind logins or Cloudflare walls
        "glassdoor.com",
        "glassdoor.co.uk",
        "indeed.com",
        "ziprecruiter.com",
        "dice.com",
        # Content farms / salary databases / career advice — never contain real JDs.
        # These appear in DDG search results and waste fetch attempts.
        "dailyremote.com",
        "jobted.com",
        "h1bdata.info",
        "h1bdata.net",
        "mastersindatascience.org",
        "careerjet.com",
        "syntaxacademy.com",
        "mrxjobs.com",
        "beamjobs.com",  # resume examples, not JDs
        "freelancer.co.uk",  # freelance marketplace, not JDs
        "simplyhired.com",  # gated aggregator (403s on direct fetch)
        "workopolis.com",  # gated aggregator (403s on direct fetch)
        "talent.com",  # job aggregator search results, not JDs
        "regionalhelpwanted.com",  # job aggregator
        "fishbowlapp.com",  # gated social network (403s)
        "thehomebase.ai",  # job aggregator listings
        "imogate.com",  # generic job listings
        "bigdatakb.com",  # job aggregator/scraper
        "role.com",  # AI job-search aggregator; republishes other employers' postings
    }
)

# ---------------------------------------------------------------------------
# Priority domains: sites that reliably serve full JDs when fetched.
# ORDER IS LOAD-BEARING — domain_priority() uses enumerate() so index 0 is
# highest priority. ATS platforms (direct API-backed pages) come first, then
# LinkedIn public job pages, then general job boards.
# ---------------------------------------------------------------------------

# Non-ATS job boards that have no PlatformSpec entry (kept as residual constant).
# These are appended after the ATS platforms derived from the registry.
_NON_ATS_PRIORITY_DOMAINS: list[str] = [
    "linkedin.com/jobs",  # LinkedIn public job pages (Playwright fetch)
    "builtin.com",  # Tech-focused job board
    "workingnomads.com",  # Remote-focused job board
    "ycombinator.com/companies",  # YC company listings with JDs
]


# PRIORITY_DOMAINS is built from the registry-derived ATS domains (by priority order)
# plus the non-ATS residual above. Import-time construction to avoid circular import.
# The ATS portion is derived from ats_registry.PRIORITY_DOMAINS_ATS.
def _build_priority_domains() -> list[str]:
    from jobcannon.engine.ats_registry import PRIORITY_DOMAINS_ATS

    return PRIORITY_DOMAINS_ATS + _NON_ATS_PRIORITY_DOMAINS


PRIORITY_DOMAINS: list[str] = _build_priority_domains()


# ---------------------------------------------------------------------------
# Aggregator / non-ATS job-board host set — consulted by careers-page discovery
# (careers_scraper.find_careers_url), NOT by the JD-fetch pipeline. A company's
# careers_url must point at the EMPLOYER's own site (or its ATS), never a
# third-party job board, so a homepage footer link to the company's own
# LinkedIn / BuiltIn / Glassdoor "jobs" page must be rejected before it is
# persisted (the role.com aggregator-pollution class).
#
# Host portions of the non-ATS job boards already declared in
# _NON_ATS_PRIORITY_DOMAINS (linkedin.com, builtin.com, ...) — DERIVED from that
# list, never a second hardcoded copy, so a new job board added there is covered
# here automatically. The composite "linkedin.com/jobs" collapses to its host
# label "linkedin.com" (any LinkedIn path is a non-employer careers target).
# ---------------------------------------------------------------------------
_NON_ATS_JOB_BOARD_HOSTS: frozenset[str] = frozenset(
    entry.split("/", 1)[0] for entry in _NON_ATS_PRIORITY_DOMAINS
)

# Union the careers-page discovery negative gate matches against: gated
# aggregators / content farms (BLOCKED_DOMAINS) PLUS the non-ATS job boards
# (linkedin, builtin, ...) that is_blocked_domain deliberately omits.
_AGGREGATOR_OR_JOB_BOARD_HOSTS: frozenset[str] = BLOCKED_DOMAINS | _NON_ATS_JOB_BOARD_HOSTS


# ---------------------------------------------------------------------------
# Public helper functions
# ---------------------------------------------------------------------------


def is_blocked_domain(url: str) -> bool:
    """Return True if the URL's hostname is (a subdomain of) a blocked domain.

    Checks the **hostname only** (not the full URL string) and matches on a
    label boundary (``host == domain or host.endswith("." + domain)``) rather
    than a raw substring, so a look-alike host that merely embeds a blocked
    domain name — e.g. "notindeed.com" or "indeed.com.evil.example" — is not
    misclassified as blocked (the ``py/incomplete-url-substring-sanitization``
    anti-pattern), and a legitimate path that happens to contain a blocked
    domain name as a word (e.g. "https://acme.com/jobs/glassdoor-reviews")
    still is NOT blocked.

    Used by both the free-tier pipeline (data_enricher) and the agentic enricher
    to skip URLs that reliably return auth walls or Cloudflare challenges.

    Args:
        url: Any URL string (may be empty).

    Returns:
        True if the URL should be skipped; False if safe to fetch.
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host_lower = host.lower()
    return any(
        host_lower == domain or host_lower.endswith("." + domain) for domain in BLOCKED_DOMAINS
    )


def is_aggregator_or_job_board(url: str) -> bool:
    """Return True if the URL's host is a known aggregator OR non-ATS job board.

    Union of BLOCKED_DOMAINS (gated aggregators / content farms — glassdoor,
    indeed, ziprecruiter, dice, role.com, ...) and the non-ATS job-board hosts
    derived from PRIORITY_DOMAINS (linkedin, builtin, workingnomads,
    ycombinator). Host-boundary matched (exact or subdomain) against the PARSED
    hostname — never a raw substring of netloc/URL — so a look-alike host
    ("linkedin.com.evil.example") or a domain embedded in a path/query cannot
    be misclassified (the ``py/incomplete-url-substring-sanitization`` guard,
    same shape as is_blocked_domain).

    Deliberately DISTINCT from is_blocked_domain: that predicate gates free-tier
    JD *fetching*, so it excludes linkedin.com (whose guest JD pages are
    fetchable) and never listed builtin.com (a priority JD source). Careers-page
    *discovery* needs the opposite stance — a company's own LinkedIn / BuiltIn /
    Glassdoor "jobs" page (its footer link often carries a ``/jobs`` path) must
    never be persisted as the company's careers_url, because that republishes a
    multi-employer listing host to the careers crawler, ATS discovery, and the
    Apply button (the role.com aggregator-pollution class). Consulted by
    careers_scraper.find_careers_url via _is_disqualified_careers_host.
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(
        host == domain or host.endswith("." + domain) for domain in _AGGREGATOR_OR_JOB_BOARD_HOSTS
    )


def domain_priority(url: str) -> int:
    """Return a priority rank for a URL (lower = higher priority).

    Iterates PRIORITY_DOMAINS with enumerate(); returns the index of the first
    matching entry, or 100 if no match. Callers sort ascending so that ATS
    platforms (index 0–4) are tried before general job boards.

    Each PRIORITY_DOMAINS entry is either a bare domain ("greenhouse.io") or a
    domain plus a path segment ("linkedin.com/jobs"). Matching is bound to the
    URL's PARSED hostname (label-boundary: exact or subdomain) and, for entries
    with a path segment, the parsed path — never a raw substring of the whole
    URL, which a look-alike host or a domain embedded in an unrelated query
    param/path could spoof (the ``py/incomplete-url-substring-sanitization``
    anti-pattern; e.g. "https://evil.com/r?u=greenhouse.io/apply" must not rank
    as greenhouse).

    Args:
        url: Any URL string.

    Returns:
        Integer priority rank. 0 = highest priority; 100 = unknown domain.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
    except Exception:
        return 100
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().lstrip("/")
    for i, domain in enumerate(PRIORITY_DOMAINS):
        domain_host, _, domain_path = domain.partition("/")
        if host != domain_host and not host.endswith("." + domain_host):
            continue
        if domain_path and not (path == domain_path or path.startswith(domain_path + "/")):
            continue
        return i
    return 100
