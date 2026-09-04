# PORTED from job_finder/web/agentic_enricher.py @ 307c369c0688763a18c6989adb81b229928e20d0 (private job-cannon). Ledger L-0132.
"""Agentic job description enricher using Ollama + DDG search + Playwright.

Recovers job descriptions for 'exhausted' jobs where the standard enrichment
pipeline failed. Uses a multi-step agentic loop:

1. Ollama generates targeted search queries from job metadata
2. DDG search finds candidate URLs (free, no API key needed)
3. Playwright fetches pages with JS rendering
4. Ollama validates whether fetched content is the right job posting
5. Extracts and persists jd_full on success

Batch-only by design (Playwright is heavy): ONE Chromium is launched and reused
across every row in a run. There is deliberately no per-row entry point — the
former ``enrich_one_job`` was removed because the synchronous enrich_job cascade
called it per row and spawned a fresh browser each time. Requires: Ollama running
locally, Playwright + Chromium installed.

# PORT-SEAM: this row lands ADAPT per the signed ledger verdict (L-0132), not the
# design note's own §5-Q1 recommendation to flip it to DIES -- a design note
# cannot override a signed Gate-2 verdict, and the owner has not overruled this
# one. The module shares the LLM-steered-Playwright mechanism the deleted
# ai-nav tier used and has no public consumer. GATED OFF BY DEFAULT: no call
# site in this repo wires run_agentic_backfill, and ScanServices.call_model is
# None until a host constructs one -- both the Playwright loop and the
# call_model threading below are dead code until a future row adds a caller.
# See the PR body's Design conformance section.
#
# PORT-SEAM: call_model is threaded as a REQUIRED keyword-only parameter on
# every function that needs it (matches jobcannon.engine.job_scorer.score_job's
# precedent) rather than read from get_services() inside this module -- the
# eventual host-side caller of run_agentic_backfill reads
# get_services().call_model once at ITS entry point and passes it down here,
# mirroring services.py's own call_model field comment ("crawler/enricher/
# nightly consumers ... thread it down explicitly as a parameter ... do NOT
# each read ScanServices themselves").
#
# PORT-SEAM: 5 helper functions this module needs (fetch_linkedin_jd,
# is_chrome_or_login_page, is_short_auth_page, company_name_in_text,
# company_tokens) live in job_finder/web/enrichment_tiers.py privately, which
# is ledger row L-0178 -- unlanded, and the design note gives this module no
# seam for them (its own fidelity table marks agentic_enricher as "recommend
# DIES", so it never designed one). Per the boundary-guard rule's second
# option ("include the module in this unit and say so in the PR body"), the 5
# functions plus their 4 supporting constants are inlined below as private
# module-scope helpers rather than imported, each block separately marked
# PORT-SEAM. This mirrors the existing precedent for borrowing a helper from a
# DIFFERENT private module into a port (services.py's load_careers_override /
# TRIGGER_PREFIX_CAREERS_URL / DEFAULT_MAX_BOARD comments). See the PR body's
# Modularity note for the de-duplication follow-up once L-0178 lands.

Usage:
    from jobcannon.engine.agentic_enricher import run_agentic_backfill
    from jobcannon.engine.services import get_services
    svc = get_services()
    count = run_agentic_backfill(config, limit=50, call_model=svc.call_model)

# PORT-SEAM: db_path dropped from run_agentic_backfill's signature --
# svc.connection_factory() is zero-arg (matches careers_crawler/_persistence.py's
# L-0465 precedent), so the Usage example above is not byte-identical to the
# private docstring's ``run_agentic_backfill("jobs.db", config, limit=50)``.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

# PORT-SEAM: Callable added for the injected call_model parameter (see module docstring)
from typing import Any, Callable

# PORT-SEAM: DEFAULT_AGENTIC_BATCH_LIMIT / JD_STORAGE_MAX_CHARS /
# get_agentic_exhausted_retry_policy import dropped -- inlined below (see
# "Config" section) / JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars.
from jobcannon.engine.jd_content_contract import JdVerdict, classify_jd_content

# PORT-SEAM: db._jd_full.set_jd_full import dropped -- svc.set_jd_full
# (ScanServices seam) is used instead (see run_agentic_backfill).
# PORT-SEAM: db._queries._SUB_SCORE_SUM_SQL import dropped -- inlined
# verbatim below (see run_agentic_backfill section) -- no host counterpart.
from jobcannon.engine.enrichment_states import EnrichmentTier
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine._http_constants import _REQUEST_TIMEOUT, _TIMEOUT

# PORT-SEAM: new import -- ScanServices.get_services() seam (L-0132)
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PORT-SEAM: Config inlined from job_finder/config.py -- pure config-parsing
# with no host/DB dependency, no existing ScanServices seam covers it, and no
# call site in this port needs a host-tunable override. Mirrors the
# careers_source_key / TRIGGER_PREFIX_CAREERS_URL precedent of copying a pure
# helper in verbatim rather than inventing a seam for it.
# ---------------------------------------------------------------------------

DEFAULT_AGENTIC_BATCH_LIMIT = 50
DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS = 30
DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES = 2


def get_agentic_exhausted_retry_policy(config: dict | None = None) -> tuple[int, int]:
    """Resolve the agentic_exhausted retry/expiry policy from config.

    Returns:
        (cooldown_days, max_retries) with safe defaults. Both are clamped to
        >= 0 (a malformed negative config value falls back to the default
        rather than producing a policy that retries every sweep or never
        retries at all).
    """
    if config is None:
        config = {}
    agentic_cfg = config.get("agentic", {}) or {}
    cooldown_days = int(
        agentic_cfg.get("retry_cooldown_days", DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS)
    )
    max_retries = int(agentic_cfg.get("retry_max_attempts", DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES))
    if cooldown_days < 0:
        cooldown_days = DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS
    if max_retries < 0:
        max_retries = DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES
    return cooldown_days, max_retries


def _resolve_batch_limit(config: dict, limit: int | None) -> int:
    """Resolve the agentic per-run job cap.

    An explicit ``limit`` (e.g. from the one-shot CLI) always wins. When
    ``limit is None`` (the scheduled-job path), read ``agentic.batch_limit``
    from config, falling back to ``DEFAULT_AGENTIC_BATCH_LIMIT``.
    """
    if limit is not None:
        return limit
    try:
        return int(config.get("agentic", {}).get("batch_limit", DEFAULT_AGENTIC_BATCH_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_AGENTIC_BATCH_LIMIT


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# PORT-SEAM: private ``_MAX_JD_CHARS = JD_STORAGE_MAX_CHARS`` module constant
# dropped -- ScanServices.jd_storage_max_chars is a required host-tunable
# field with an existing seam (data_enricher.py / _scraper_extract.py /
# careers_scraper.py all read it the same way), so usage sites below call
# ``get_services().jd_storage_max_chars`` instead of a module-level alias.
_MAX_SEARCH_QUERIES = 4
_MAX_URLS_PER_QUERY = 3
_MAX_FETCH_ATTEMPTS = 6  # Total URLs to try before giving up
_PAGE_LOAD_WAIT_MS = 3000
# Ollama context window limit for validation. Intentionally less than _MAX_JD_CHARS
# (8000) because the validator prompt already consumes tokens and we want to leave
# room for the model's JSON response without truncating mid-reasoning.
_VALIDATE_MAX_CHARS = 6000


# Social-surface URL path patterns — path-level complement to domain_policy's hostname-level
# blocklist. linkedin.com/jobs/ is a valid JD source; linkedin.com/posts/ is a social post
# that reliably yields junk content. These patterns are matched against the full URL string
# (lowercase) so the path can be checked without a urlparse() call per URL.
_SOCIAL_POST_URL_PATTERNS: tuple[str, ...] = (
    "linkedin.com/posts/",
    "linkedin.com/feed/",
    "twitter.com/status/",
    "x.com/status/",
    "facebook.com/permalink/",
    "threads.net/t/",
)


def _is_social_post_url(url: str) -> bool:
    """Return True if *url* points to a social-media post (not a JD page).

    Path-level filter, complementing the hostname-level is_blocked_domain().
    linkedin.com/jobs/ pages are valid JD sources and are intentionally NOT
    filtered — only social-content path shapes (posts, feeds, status) are blocked.
    """
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in _SOCIAL_POST_URL_PATTERNS)


# System prompt for query generation
_QUERY_GEN_PROMPT = """\
You are a job search assistant. Given a job title and company name, generate \
{n} different web search queries that are likely to find the full job description \
posted on the company's careers page, a job board, or LinkedIn.

Rules:
- Each query should use a DIFFERENT search strategy
- Include the company name and key title words
- Try: company careers page, LinkedIn, Greenhouse/Lever, job boards
- Use quotes around multi-word phrases when helpful
- Output ONLY a JSON array of strings, no explanation

Example output: ["Uber careers Data Analyst Measurement Science", "site:linkedin.com Uber Data Analyst Ads"]
"""

# System prompt for page validation
_VALIDATE_PROMPT = """\
You are validating whether a web page contains the job description for a specific role.

Target job: {title} at {company}

Analyze the text below and respond with ONLY a JSON object:
{{
  "is_match": true/false,
  "confidence": 0.0-1.0,
  "reason": "brief explanation"
}}

Set is_match=true ONLY if the page contains the SPECIFIC duties, responsibilities, or \
requirements of THIS role — not just a mention of the title or company somewhere on the \
page. Allow minor title variations (e.g., "Sr" vs "Senior", "Lead" vs "Staff").

Set is_match=false if the page is: a different role, a job-listing/search-results index \
page (many jobs, not one), a generic careers landing page with no specific posting body, \
a "no results" / "no jobs match your search" page, an unfilled CMS template (placeholder \
text like "your subtitle goes here", "lorem ipsum"), a company About/marketing page, a \
login/blocked/captcha page, or unrelated content. A confident match requires the page's \
OWN body to describe the role's work — the mere presence of the company name or a \
"similar jobs" widget listing OTHER openings is not evidence of a match.
"""


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_ddg(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo and return results [{title, href, body}].

    When DDGS exhausts every engine (google, yandex, yahoo, grokipedia, ...)
    it returns an empty list without raising. Surface as INFO (greppable as
    'DDGS: all engines returned empty') — not actionable for the operator
    and high-volume (60+ per audit week), so WARNING was misleading: the
    pipeline has its own fallback to DataForSEO / Google CSE and degrades
    gracefully when DDGS can't help.
    """
    try:
        from ddgs import DDGS

        with DDGS(timeout=_TIMEOUT) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        logger.debug("DDG search failed for '%s': %s", query[:60], exc)
        return []

    if not results:
        logger.info("DDGS: all engines returned empty for query '%s'", query[:80])
    return results


def _rank_urls(search_results: list[dict]) -> list[str]:
    """Extract, deduplicate, filter, and rank URLs from search results.

    Uses is_blocked_domain and domain_priority from the centralized domain_policy
    module rather than the previously duplicated local _BLOCKED_DOMAINS /
    _PRIORITY_DOMAINS constants. This ensures all callers share the same policy.
    """
    # Imported here to keep module-level imports clean and avoid circular refs
    from jobcannon.engine.domain_policy import domain_priority, is_blocked_domain

    seen = set()
    urls = []
    for r in search_results:
        href = r.get("href", "")
        if not href or href in seen or is_blocked_domain(href) or _is_social_post_url(href):
            continue
        seen.add(href)
        urls.append(href)

    # Sort by domain priority: lower index = higher priority (ATS platforms first)
    urls.sort(key=domain_priority)
    return urls


# ---------------------------------------------------------------------------
# PORT-SEAM: enrichment_tiers helpers inlined below -- see module docstring's boundary-
# guard note above -- these 5 functions and their 4 supporting constants are
# copied verbatim from job_finder/web/enrichment_tiers.py @ 307c369c (ledger
# L-0178, unlanded) because this port's own row has no other sanctioned way to
# reach them. Follow-up: de-duplicate once L-0178 lands (see PR body).
# ---------------------------------------------------------------------------

# Browser-like headers for sites that block bot UAs
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Chrome/login page detection signals
_CHROME_SIGNALS = [
    "download google chrome",
    "update your browser",
    "browser not supported",
    "enable cookies",
    "cookies are disabled",
    "accept cookies to continue",
]

_LOGIN_PAGE_SIGNALS = [
    "create your free account",
    "sign up for free",
    "start your free trial",
    "register to view",
    "join now to view",
]

_COMPANY_STOP_WORDS = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "co",
        "the",
        "and",
        "group",
        "holdings",
        "international",
        "services",
        "solutions",
        "technologies",
    }
)


def is_short_auth_page(text: str) -> bool:
    """Return True if text looks like a short auth-wall or CAPTCHA page.

    Detection: page is under 2000 chars AND the first 500 chars contain
    an auth/bot signal keyword.
    """
    if not text or len(text) >= 2000:
        return False
    prefix = text[:500].lower()
    signals = [
        "sign in",
        "log in",
        "login",
        "captcha",
        "just a moment",
        "access denied",
        "verify you are human",
        "verify you are a human",
    ]
    return any(s in prefix for s in signals)


def company_tokens(company_name: str) -> list[str]:
    """Extract meaningful tokens from a company name, filtering stop words.

    Returns lowercase tokens that are >= 2 chars and not in the stop list.
    """
    if not company_name:
        return []
    raw_tokens = re.split(r"[\s.,;:!?&/|()]+", company_name.lower())
    return [t for t in raw_tokens if len(t) >= 2 and t not in _COMPANY_STOP_WORDS]


def company_name_in_text(company_name: str, text: str) -> bool:
    """Check whether any meaningful company token appears in the text."""
    tokens = company_tokens(company_name)
    if not tokens:
        return False
    text_lower = text.lower()
    return any(t in text_lower for t in tokens)


def is_chrome_or_login_page(text: str) -> bool:
    """Return True if text looks like a browser upgrade or login/signup page.

    Checks for Chrome download prompts, browser upgrade notices, cookie
    consent walls, and generic signup gates.

    Args:
        text: Cleaned page text to check.

    Returns:
        True if the page is a Chrome/browser page or login gate.
    """
    if not text:
        return False

    text_lower = text[:2000].lower()
    if any(sig in text_lower for sig in _CHROME_SIGNALS):
        return True
    return bool(any(sig in text_lower for sig in _LOGIN_PAGE_SIGNALS))


def fetch_linkedin_jd(url: str) -> str | None:
    """Extract job description from a LinkedIn guest job page.

    LinkedIn guest pages serve full JD content inside a specific container
    even though the surrounding page chrome contains login prompts that
    trip the generic auth-wall detector.

    Args:
        url: A LinkedIn job URL (e.g. linkedin.com/jobs/view/...).

    Returns:
        Cleaned JD text up to jd_storage_max_chars, or None if extraction fails.
    """
    # PORT-SEAM: requests / fetch_with_deadline / extract_clean_jd imported
    # locally (private module-level) -- fetch_with_deadline and
    # extract_clean_jd are already-landed engine imports; requests is a
    # direct third-party dependency, matching the rest of this module's
    # lazy-import style for network helpers.
    import requests

    from jobcannon.engine.http_fetch import fetch_with_deadline
    from jobcannon.engine.platform_extractor import extract_clean_jd

    try:
        response = fetch_with_deadline(
            url, getter=requests.get, headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT
        )
        response.raise_for_status()

        # LinkedIn scoping now lives in the single chokepoint (extract_clean_jd
        # selects div.show-more-less-html__markup / div.description__text and
        # strips page chrome). This function stays as the LinkedIn-specific
        # entry point — browser headers + the existing callers (DDG tier, the
        # agentic Playwright shortcut) — but delegates the actual extraction so
        # there is exactly one definition of "what a LinkedIn JD looks like".
        text = extract_clean_jd(url, response.text)
        if not text or not text.strip():
            logger.debug("LinkedIn JD container not found for '%s'", url)
            return None

        return text[: get_services().jd_storage_max_chars]

    except Exception as e:
        logger.debug("LinkedIn JD fetch failed for '%s': %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Page fetching (Playwright)
# ---------------------------------------------------------------------------


def _create_browser(playwright):
    """Create a Playwright browser context with realistic fingerprint."""
    browser = playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    page = ctx.new_page()
    page.add_init_script('Object.defineProperty(navigator, "webdriver", {get: () => undefined})')
    return browser, page


def _wait_for_networkidle(page, timeout_ms: int) -> None:
    """Wait up to *timeout_ms* for the page network to become idle.

    Early-exits when the network goes quiet; swallows the Playwright timeout
    if the deadline is reached instead (matching the old fixed-delay
    behavior). Playwright is not a core dependency here — it's only
    transitively available via the dev extra's pytest-playwright — so the
    timeout exception type is imported lazily and guarded, rather than at
    module load. Mirrors careers_crawler/_playwright_tier.py's
    ``_wait_for_js_settle`` idiom exactly.
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        PlaywrightTimeoutError = Exception
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def _fetch_page_text(page, url: str, timeout_ms: int = 15000) -> str | None:
    """Fetch a URL with Playwright and return cleaned text content.

    Routes the rendered HTML through ``platform_extractor.extract_clean_jd``
    (platform-scoped container + page-chrome strip; the single chokepoint).
    Auth-wall detection via is_short_auth_page() and is_chrome_or_login_page().
    LinkedIn URLs are tried with the lightweight fetch_linkedin_jd() extractor
    first (no Playwright needed). Falls through to Playwright if that fails.
    """
    svc = get_services()  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars

    # LinkedIn shortcut: try lightweight extractor first (no Playwright needed)
    if "linkedin.com/jobs/" in url:
        try:
            # PORT-SEAM: fetch_linkedin_jd is the inlined enrichment_tiers
            # helper above (L-0178 boundary-guard block), not an import.
            li_text = fetch_linkedin_jd(url)
            if li_text and len(li_text) >= 300:
                return li_text[
                    : svc.jd_storage_max_chars * 2
                ]  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
        except Exception as exc:
            logger.debug("LinkedIn lightweight extractor failed for %s: %s", url[:80], exc)
        # Fall through to Playwright if LinkedIn extractor fails

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _wait_for_networkidle(page, _PAGE_LOAD_WAIT_MS)

        html = page.content()

        # PORT-SEAM: is_chrome_or_login_page / is_short_auth_page are the
        # inlined enrichment_tiers helpers above, not an import.
        from jobcannon.engine.platform_extractor import extract_clean_jd

        # Single chokepoint: platform-scoped + chrome-stripped extraction. Passing
        # the URL lets a Playwright-rendered LinkedIn page be scoped to its JD
        # container instead of stored whole (JD + similar-jobs + footer chrome).
        text = extract_clean_jd(url, html)
        if not text:
            return None

        if is_short_auth_page(text):
            logger.debug("Short auth-wall detected on %s", url[:80])
            return None

        if is_chrome_or_login_page(text):
            logger.debug("Chrome/login page detected on %s", url[:80])
            return None

        return text[
            : svc.jd_storage_max_chars * 2
        ]  # Keep extra for validation, trim later; # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars

    except Exception as exc:
        logger.debug("Playwright fetch failed for %s: %s", url[:80], exc)
        return None


# ---------------------------------------------------------------------------
# OllamaProvider-backed LLM calls
# _call_ollama() deleted — all LLM calls now go through OllamaProvider which
# is instantiated once in run_agentic_backfill() and passed down. This ensures:
# 1. Consistent routing through the multi-provider infrastructure
# 2. ModelResult.data is already parsed JSON (no redundant json.loads())
# 3. Health check happens exactly once at startup (not per-job)
# ---------------------------------------------------------------------------


def _generate_queries(
    title: str,
    company: str,
    n: int,
    conn: Any,
    config: dict,
    *,
    call_model: Callable[
        ..., Any
    ],  # PORT-SEAM: injected model-dispatch callable (was model_provider.call_model)
) -> list[str]:
    """Generate search queries for a job posting using call_model.

    Args:
        title: Job title.
        company: Company name.
        n: Number of queries to generate.
        conn: SQLite connection for cost recording.
        config: Application config dict for provider routing.
        call_model: Injected model-dispatch callable (required).
        # PORT-SEAM: injected, not read via get_services() here (see module docstring)

    Returns:
        List of search query strings. Falls back to heuristic queries on failure.
    """
    # PORT-SEAM: model_provider.call_model import dropped -- call_model is now
    # an injected parameter (see signature above), not imported here.
    system = _QUERY_GEN_PROMPT.format(n=n)
    user_msg = f"Job title: {title}\nCompany: {company}"

    # Inner try/except: handles mid-run transient failures (model timeout,
    # malformed JSON from a specific query) without crashing the outer loop.
    try:
        result = call_model(
            tier="quick",
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            conn=conn,
            config=config,
            job_id=None,
            purpose="agentic_query_generation",
            max_tokens=512,
        )
        data = result.data
    except Exception as exc:
        logger.warning(
            "call_model error in _generate_queries for '%s' @ '%s': %s — "
            "falling back to heuristic queries",
            title[:40],
            company[:20],
            exc,
        )
        return _fallback_queries(title, company)

    # Handle both list and dict response shapes
    if isinstance(data, list) and all(isinstance(q, str) for q in data):
        return data[:n]
    if isinstance(data, dict):
        for key in ("queries", "search_queries", "results"):
            if key in data and isinstance(data[key], list):
                return [str(q) for q in data[key][:n]]

    return _fallback_queries(title, company)


def _fallback_queries(title: str, company: str) -> list[str]:
    """Generate basic search queries without AI."""
    # Strip parentheticals from title
    clean_title = re.sub(r"\([^)]*\)", "", title).strip()
    return [
        f"{company} careers {clean_title}",
        f'"{clean_title}" "{company}" job description',
        f"site:linkedin.com {company} {clean_title}",
        f"site:greenhouse.io OR site:lever.co {company} {clean_title}",
    ]


def _validate_page(
    text: str,
    title: str,
    company: str,
    conn: Any,
    config: dict,
    *,
    call_model: Callable[
        ..., Any
    ],  # PORT-SEAM: injected model-dispatch callable (was model_provider.call_model)
) -> tuple[bool, float]:
    """Validate whether page content matches the target job using call_model.

    Args:
        text: Page text to validate (will be truncated to keep context reasonable).
        title: Target job title.
        company: Target company name.
        conn: SQLite connection for cost recording.
        config: Application config dict for provider routing.
        call_model: Injected model-dispatch callable (required).
        # PORT-SEAM: injected, not read via get_services() here (see module docstring)

    Returns:
        Tuple of (is_match, confidence). Returns (False, 0.0) on any failure.
    """
    # PORT-SEAM: model_provider.call_model import dropped -- call_model is now
    # an injected parameter (see signature above), not imported here.
    system = _VALIDATE_PROMPT.format(title=title, company=company)
    # Truncate page text to _VALIDATE_MAX_CHARS (not _MAX_JD_CHARS) to leave
    # token budget for the model's JSON response without truncating mid-reasoning.
    user_msg = text[:_VALIDATE_MAX_CHARS]

    # Inner try/except: handles mid-run transient failures per-URL
    try:
        result = call_model(
            tier="quick",
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            conn=conn,
            config=config,
            job_id=None,
            purpose="agentic_page_validation",
            max_tokens=256,
        )
        data = result.data
    except Exception as exc:
        logger.warning("call_model error in _validate_page: %s", exc)
        return False, 0.0

    try:
        is_match = bool(data.get("is_match", False))
        confidence = float(data.get("confidence", 0.0))
        reason = data.get("reason", "")
        if reason:
            logger.debug("Validation: match=%s conf=%.2f reason=%s", is_match, confidence, reason)
        return is_match, confidence
    except (TypeError, ValueError, AttributeError):
        return False, 0.0


# ---------------------------------------------------------------------------
# Main agentic loop (per job)
# ---------------------------------------------------------------------------


def enrich_single_job(
    job_row: dict,
    page,
    conn: Any,
    config: dict,
    *,
    call_model: Callable[
        ..., Any
    ],  # PORT-SEAM: injected model-dispatch callable (was model_provider.call_model)
) -> str | None:
    """Run the agentic enrichment loop for a single job.

    Args:
        job_row: Job dict with title, company fields.
        page: Playwright page object (reused across jobs).
        conn: SQLite connection for cost recording.
        config: Application config dict for provider routing.
        call_model: Injected model-dispatch callable (required).
        # PORT-SEAM: injected, not read via get_services() here (see module docstring)

    Returns:
        The job description text if found, None otherwise.
    """
    title = job_row.get("title", "")
    company = job_row.get("company", "")

    if not title or not company:
        return None

    # Step 1: Generate search queries via call_model
    # PORT-SEAM: call_model=call_model threaded through (injected parameter)
    queries = _generate_queries(
        title, company, n=_MAX_SEARCH_QUERIES, conn=conn, config=config, call_model=call_model
    )
    logger.info("Agentic: %d queries for '%s' @ '%s'", len(queries), title[:40], company[:20])

    # Step 2: Search and collect candidate URLs
    # The queries are independent; run them concurrently and merge their
    # results in the original query order before ranking/deduplication.
    all_urls: list[str] = []
    with ThreadPoolExecutor(max_workers=_MAX_SEARCH_QUERIES) as executor:
        futures = [
            executor.submit(_search_ddg, query, max_results=_MAX_URLS_PER_QUERY)
            for query in queries
        ]
        for future in futures:
            try:
                results = future.result()
            except Exception as exc:
                logger.debug("DDG search failed: %s", exc)
                results = []
            urls = _rank_urls(results)
            all_urls.extend(u for u in urls if u not in all_urls)

    if not all_urls:
        logger.info("Agentic: no URLs found for '%s' @ '%s'", title[:40], company[:20])
        return None

    logger.info("Agentic: %d candidate URLs", len(all_urls))

    # Step 3: Fetch and validate pages
    best_text: str | None = None
    best_confidence: float = 0.0

    # Failure reason counters for observability
    fetch_ok = 0
    company_miss = 0
    low_conf = 0
    auth_walls = 0
    content_rejected = 0
    shape_rejected = 0

    for _i, url in enumerate(all_urls[:_MAX_FETCH_ATTEMPTS]):
        text = _fetch_page_text(page, url)
        if not text:
            auth_walls += 1
            continue

        fetch_ok += 1

        # Deterministic content-contract pre-filter — the SAME jd_content_reject
        # signals set_jd_full enforces at the storage gate (wiki/bot-wall, listing
        # index, 404, CMS placeholder scaffold, "no results for your search"
        # chrome, expired, title-zero-overlap), computed once via
        # classify_jd_content() and reused below for the CLEAN-bar acceptance
        # check. Checked here, before the LLM validate call, for two reasons:
        # (1) skip the inference cost on a page that is already provably not a
        # JD; (2) a deterministically-junk page must never win the "best match"
        # slot and `break` the loop early — that would strand the search on a
        # bare careers-landing page instead of trying the next candidate URL,
        # which is exactly how a real posting can be missed in favor of
        # confidently-wrong CMS scaffold text.
        content_verdict = classify_jd_content(text, title=title, company=company, config=config)
        if content_verdict.verdict is JdVerdict.REJECT:
            content_rejected += 1
            continue

        # Quick heuristic: verify at least one meaningful company token appears in
        # the page before paying Ollama inference cost.
        # Uses shared company_tokens() + company_name_in_text() from enrichment_tiers
        # (same logic used by fetch_ddg_jds for DDG tier validation).
        # PORT-SEAM: company_name_in_text / company_tokens are the inlined
        # enrichment_tiers helpers above, not an import.
        tokens = company_tokens(company)
        if not tokens:
            # DEFECT 015 FIX: fail CLOSED — degenerate company name (all stop-words).
            # Skip rather than burn inference budget on a heuristic that cannot operate.
            logger.debug(
                "Agentic: skipping %s (company '%s' yields no meaningful tokens)",
                url[:60],
                company[:30],
            )
            company_miss += 1
            continue
        if not company_name_in_text(company, text):
            # Bypass for long pages with short company names — worth the Ollama cost
            if len(tokens) <= 2 and len(text) > 2000:
                logger.debug("Agentic: bypassing company check for long page %s", url[:60])
            else:
                logger.debug("Agentic: skipping %s (company name not found)", url[:60])
                company_miss += 1
                continue

        # Validate with call_model
        # PORT-SEAM: call_model=call_model threaded through (injected parameter)
        is_match, confidence = _validate_page(
            text, title, company, conn=conn, config=config, call_model=call_model
        )

        if is_match and confidence > best_confidence:
            # The LLM alone can be fooled by a generic careers-landing template:
            # it mentions the company, is long enough to look substantial, and
            # can even share an incidental title token (e.g. a company-wide
            # "Engineering" jobs list) without being THIS posting's body.
            # Require the same deterministic CLEAN bar the jd content contract
            # uses downstream (a JD-shape signal, grounded in the job's own
            # title, and a substantial length) before a page can win the "best
            # match" slot — posting-specific corroboration, not just the LLM's
            # say-so.
            if content_verdict.verdict is not JdVerdict.CLEAN:
                shape_rejected += 1
            else:
                best_text = text
                best_confidence = confidence
                if confidence >= 0.8:
                    logger.info("Agentic: high-confidence match at %s (%.2f)", url[:60], confidence)
                    break
        elif not is_match:
            low_conf += 1

    # Log failure breakdown at INFO level for observability
    logger.info(
        "Agentic: '%s' @ '%s' — urls=%d, fetched=%d, content_rejected=%d, "
        "shape_rejected=%d, company_mismatch=%d, low_confidence=%d, auth_wall=%d",
        title[:40],
        company[:20],
        len(all_urls),
        fetch_ok,
        content_rejected,
        shape_rejected,
        company_miss,
        low_conf,
        auth_walls,
    )

    if best_text and best_confidence >= 0.5:
        # Trim to JD limit
        return best_text[
            : get_services().jd_storage_max_chars
        ]  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars

    return None


# NOTE: the former per-row ``enrich_one_job`` entry point was removed
# (2026-06-22). It launched a fresh Chromium on every call, and the synchronous
# enrich_job cascade invoked it per row inside the uncapped run_enrichment_backfill
# loop — a process-spawn storm. The agentic tier now runs ONLY via the batched
# ``run_agentic_backfill`` below (one browser reused across all rows), driven both
# inline at the tail of run_enrichment_backfill and nightly by the scheduler.


# ---------------------------------------------------------------------------
# agentic_exhausted retry / expiry (T2.9 / D21)
# ---------------------------------------------------------------------------


def requeue_or_expire_agentic_exhausted(conn: Any, config: dict) -> tuple[int, int]:
    """Bounded retry / terminal expiry sweep for `agentic_exhausted` rows.

    Without this sweep, `agentic_exhausted` is a dead end: the row has no
    jd_full, so it can never reach scoring (`awaiting_jd` forever), and
    nothing ever looks at it again (D21 — 948 rows stuck this way, 677 older
    than 30 days). This gives a row whose posting may have become fetchable
    since a bounded number of extra chances, gated by a cooldown so the sweep
    does not retry the same dead posting on every enrichment cycle, and an
    explicit terminal `expired` state once the budget is spent so the stuck
    cohort stops being an ambiguous middle state (never an infinite retry
    loop).

    Two mutually-exclusive UPDATEs per call, both keyed on the current
    `agentic_exhausted` tier so they only ever touch rows in that exact state:

    - **Expire**: `agentic_retry_count >= max_retries` -> `enrichment_tier =
      'expired'`. No further cooldown wait — the budget is already spent, so
      there is nothing left to wait for. `expired` is in both `TERMINAL` and
      `LOW_SIGNAL_TERMINAL` (enrichment_states.py), so it is automatically
      excluded from backfill selection (`backfill_skip_sql`) and continues to
      feed the low_signal classification rule exactly like `agentic_exhausted`
      does today.
    - **Requeue**: `agentic_retry_count < max_retries` AND the cooldown has
      elapsed -> reset to `enrichment_tier = NULL` (mirrors
      `_jd_full.py::_record_jd_content_reject`'s terminal-tier reset), clear
      `agentic_exhausted_at`, and increment `agentic_retry_count`. A NULL tier
      re-enters the regular cost-ordered pipeline from the top (`resume_index`
      returns 0 for `None`), not just the agentic tier — cheaper tiers may
      have become viable too (e.g. a careers page that 404'd is back up).

    Legacy rows (pre-existing `agentic_exhausted` rows from before this
    migration shipped) have `agentic_exhausted_at IS NULL` — treated as
    cooldown-satisfied immediately (there is no real timestamp to wait on;
    their exhaustion happened long before this feature existed), so the whole
    stuck cohort becomes retry-eligible on the very first sweep rather than
    waiting another 30 days.

    # PORT-SEAM: this is EnrichmentTier.EXPIRED's writer -- jobcannon.engine.
    # enrichment_states.py:13-14 notes "EXPIRED's writer, the retry/requeue
    # sweep against a hosted LLM enricher, is not carried into this port" for
    # that L-0174 (data_enricher.py) row. This function IS that writer, landed
    # here instead, under L-0132. It has no caller in this port either (see
    # module docstring) -- see the PR body's Design conformance section.

    Args:
        conn: Open SQLite connection (short-lived, matching the per-operation
            discipline the rest of this module and data_enricher.py use).
        config: Application config dict; resolves `agentic.retry_cooldown_days`
            / `agentic.retry_max_attempts` (see `get_agentic_exhausted_retry_policy`).

    Returns:
        (requeued_count, expired_count).
    """
    cooldown_days, max_retries = get_agentic_exhausted_retry_policy(config)
    cutoff = (datetime.now(UTC) - timedelta(days=cooldown_days)).replace(tzinfo=None).isoformat()

    expire_cursor = conn.execute(
        "UPDATE jobs SET enrichment_tier = ? "
        "WHERE enrichment_tier = ? AND agentic_retry_count >= ?",
        (EnrichmentTier.EXPIRED.value, EnrichmentTier.AGENTIC_EXHAUSTED.value, max_retries),
    )
    expired_count = expire_cursor.rowcount

    requeue_cursor = conn.execute(
        "UPDATE jobs SET enrichment_tier = NULL, agentic_exhausted_at = NULL, "
        "agentic_retry_count = agentic_retry_count + 1 "
        "WHERE enrichment_tier = ? AND agentic_retry_count < ? "
        "AND (agentic_exhausted_at IS NULL OR agentic_exhausted_at <= ?)",
        (EnrichmentTier.AGENTIC_EXHAUSTED.value, max_retries, cutoff),
    )
    requeued_count = requeue_cursor.rowcount
    conn.commit()

    if requeued_count or expired_count:
        logger.info(
            "agentic_exhausted retry sweep: requeued=%d expired=%d "
            "(cooldown_days=%d, max_retries=%d)",
            requeued_count,
            expired_count,
            cooldown_days,
            max_retries,
        )
    return requeued_count, expired_count


# ---------------------------------------------------------------------------
# Batch backfill
# ---------------------------------------------------------------------------

# PORT-SEAM: inlined from job_finder/db/_queries.py -- _SUB_SCORE_SUM_SQL and
# _LOCATION_POLICY_FIT_SQL are SQLite json_extract/json_valid string SQL with
# no host counterpart: jobcannon/db/migrations/m0015_postings_scoring_tuple.py
# ("No classification_rank / sub_score_sum: private-side materialization for a
# sort path with no host consumer ... Add later, additively, if/when one
# exists") documents the columns these fragments read as deliberately NOT
# ported. Kept verbatim (SQLite-shaped) rather than rewritten against a
# Postgres jsonb shape that doesn't exist, because this row is gated OFF with
# no hosted caller -- the SELECT below is unwired dead code either way. A
# future row wiring run_agentic_backfill to a real host caller must resolve
# this against whatever scoring-tuple storage exists by then. See the PR
# body's Design conformance section.
_LOCATION_POLICY_FIT_SQL = (
    "COALESCE("
    "CASE WHEN json_valid(location_policy_verdict) = 1 "
    "THEN json_extract(location_policy_verdict, '$.effective_location_fit') END, "
    "CASE WHEN json_valid(sub_scores_json) = 1 "
    "THEN json_extract(sub_scores_json, '$.location_fit') ELSE 0 END, "
    "0)"
)

_SUB_SCORE_SUM_SQL = (
    "(CASE WHEN json_valid(sub_scores_json) = 1 THEN "
    "(COALESCE(json_extract(sub_scores_json, '$.title_fit'), 0) + "
    f"{_LOCATION_POLICY_FIT_SQL} + "
    "COALESCE(json_extract(sub_scores_json, '$.comp_fit'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.domain_match'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.seniority_match'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.skills_match'), 0)) "
    "ELSE 0 END)"
)


def run_agentic_backfill(
    # PORT-SEAM: db_path dropped -- svc.connection_factory() is zero-arg (L-0465 precedent)
    config: dict,
    limit: int | None = None,
    *,
    call_model: Callable[
        ..., Any
    ],  # PORT-SEAM: injected model-dispatch callable (was model_provider.call_model)
) -> int:
    """Run agentic enrichment on exhausted jobs missing jd_full.

    Architecture notes:
    - DB connections are scoped per-operation (short SELECT + per-job UPDATE)
      rather than held open across minutes of Playwright network I/O. This
      prevents SQLite lock contention with the Flask request thread.
    - Optimistic concurrency UPDATE prevents overwriting state changed by
      another process between SELECT and write (checks enrichment_tier = 'exhausted').

    Args:
        # PORT-SEAM: db_path dropped -- svc.connection_factory() is zero-arg
        config: Application config dict for provider routing.
        limit: Maximum jobs to process this run. When None (the scheduled-job
            path), resolved from ``agentic.batch_limit`` in config, defaulting
            to ``DEFAULT_AGENTIC_BATCH_LIMIT`` (50). An explicit value (e.g.
            from the one-shot CLI) overrides config.
        call_model: Injected model-dispatch callable (required).
        # PORT-SEAM: this row is gated OFF by default and has no caller
        # supplying this in this PR -- see the module docstring.

    Returns:
        Number of jobs successfully enriched. Always returns 0 when
        prerequisites (Playwright) are unavailable.
    """
    limit = _resolve_batch_limit(config, limit)

    # Guard: import Playwright before any DB or network work.
    try:
        from playwright.sync_api import sync_playwright

        # PORT-SEAM: db_helpers.standalone_connection import dropped here --
        # replaced by svc.connection_factory() below (L-0465 precedent).
    except ImportError as exc:
        logger.warning("Agentic backfill unavailable: %s", exc)
        return 0

    svc = get_services()  # PORT-SEAM: seam (L-0132); replaces db_helpers.standalone_connection

    # Short-lived SELECT: open connection, fetch rows, close before Playwright work.
    # Holding the connection open across minutes of network I/O is unsafe for
    # concurrent SQLite (WAL mode helps but doesn't eliminate lock contention).
    with (
        svc.connection_factory() as conn
    ):  # PORT-SEAM: db_path dropped -- svc.connection_factory() is zero-arg (L-0465 precedent)
        # v3.0 (Phase 34 Plan 3 Commit A): ORDER BY classification_rank + sub_score_sum
        # replaces ORDER BY haiku_score. Highest-priority (apply) rows processed first.
        # Guard json_extract with json_valid to handle malformed sub_scores_json (Issue #730)
        rows = conn.execute(
            f"""SELECT * FROM jobs
               WHERE enrichment_tier = 'exhausted'
                 AND jd_full IS NULL
               ORDER BY
                   CASE classification
                       WHEN 'apply'    THEN 4
                       WHEN 'consider' THEN 3
                       WHEN 'skip'     THEN 2
                       WHEN 'reject'   THEN 1
                       ELSE 0
                   END DESC,
                   {_SUB_SCORE_SUM_SQL} DESC,
                   first_seen DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    if not rows:
        # DEFECT 018 FIX: emit same structured summary as normal exit so monitoring
        # rules have a single log pattern to match ("Agentic enrichment complete").
        logger.info("Agentic enrichment complete: 0/0 jobs enriched (no exhausted jobs)")
        return 0

    total = len(rows)
    logger.info("Agentic enrichment: %d jobs to process", total)

    enriched_count = 0

    with sync_playwright() as pw:
        browser, page = _create_browser(pw)

        try:
            for i, row in enumerate(rows, 1):
                # Initialise before the inner try so the except block can always
                # reference them in log messages even if row parsing fails.
                title = "?"
                company = "?"
                try:
                    job = dict(row)
                    title = job.get("title", "?")[:55]
                    company = job.get("company", "?")[:25]
                    dedup_key = job.get("dedup_key")

                    logger.info("[%d/%d] %s @ %s", i, total, title, company)

                    t0 = time.time()
                    # Per-job conn: the outer conn from line 576's `with` block
                    # was closed at `.fetchall()`; reusing it here broke the
                    # cascade cost-recording write ("Cannot operate on a closed
                    # database"). Open a fresh short-lived conn scoped to this
                    # one job — matches the write_conn pattern below and keeps
                    # the per-operation hold the module-level note prescribes.
                    with svc.connection_factory() as enrich_conn:  # PORT-SEAM: seam (L-0132)
                        jd = enrich_single_job(
                            job, page, conn=enrich_conn, config=config, call_model=call_model
                        )
                    elapsed = time.time() - t0

                    if jd:
                        # Route the jd_full write through the content-density gate
                        # (Phase 46.03).  set_jd_full logs WARN on gate hit and
                        # returns False without writing; the enrichment_tier sibling
                        # field is updated separately in a second UPDATE below so
                        # the optimistic concurrency check (WHERE enrichment_tier =
                        # 'exhausted') remains on the tier field, not jd_full.
                        rows_updated = 0
                        with svc.connection_factory() as write_conn:  # PORT-SEAM: seam (L-0132)
                            jd_written = svc.set_jd_full(  # PORT-SEAM: seam (L-0132); replaces db._jd_full.set_jd_full
                                write_conn,
                                dedup_key,
                                jd[
                                    : svc.jd_storage_max_chars
                                ],  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
                                source="agentic_enricher",
                                # NOTE: the outer-loop `title` var above is truncated to
                                # 55 chars for log display — pass the full untruncated
                                # title here so the I-17 zero-overlap check isn't fed a
                                # clipped set of significant tokens.
                                title=job.get("title"),
                                config=config,
                            )
                            if jd_written:
                                # Per-job write connection: open, UPDATE with optimistic
                                # concurrency check, close.  The WHERE clause prevents
                                # overwriting state changed by another process between
                                # our initial SELECT and this write.
                                # DEFECT 001 FIX: capture rowcount INSIDE the `with`
                                # block before the connection closes.
                                cursor = write_conn.execute(
                                    "UPDATE jobs SET enrichment_tier = ? "
                                    "WHERE dedup_key = ? AND enrichment_tier = ?",
                                    (
                                        EnrichmentTier.AGENTIC.value,
                                        dedup_key,
                                        EnrichmentTier.EXHAUSTED.value,
                                    ),
                                )
                                write_conn.commit()
                                rows_updated = cursor.rowcount

                        if jd_written and rows_updated == 0:
                            # Another process advanced enrichment_tier between our
                            # SELECT and this UPDATE.  jd_full was still persisted.
                            logger.warning(
                                "Agentic: optimistic concurrency miss for dedup_key=%s "
                                "(JD found, %d chars, but tier changed — tier not updated)",
                                dedup_key,
                                len(jd),
                            )
                        elif jd_written:
                            enriched_count += 1
                            logger.info("  -> FOUND %d chars (%.1fs)", len(jd), elapsed)
                    else:
                        # Mark as agentic-exhausted so we don't retry immediately.
                        # agentic_exhausted_at (T2.9 / D21) anchors the bounded
                        # retry/expiry cooldown in requeue_or_expire_agentic_exhausted
                        # — stamped here so a fresh transition always has a real
                        # timestamp, not just rows this migration touches later.
                        # If rowcount == 0 here, another process already advanced the tier
                        # — skip silently (no data was found anyway, so no recovery needed).
                        with svc.connection_factory() as write_conn:  # PORT-SEAM: seam (L-0132)
                            write_conn.execute(
                                "UPDATE jobs SET enrichment_tier = ?, agentic_exhausted_at = ? "
                                "WHERE dedup_key = ? AND enrichment_tier = ?",
                                (
                                    EnrichmentTier.AGENTIC_EXHAUSTED.value,
                                    utc_now_iso(),
                                    dedup_key,
                                    EnrichmentTier.EXHAUSTED.value,
                                ),
                            )
                            write_conn.commit()
                        logger.info("  -> NOT FOUND (%.1fs)", elapsed)

                except Exception as exc:
                    # Per-job isolation: one job's failure (IntegrityError from m078
                    # trigger, Playwright error, DB lock, …) must never abort the batch.
                    # Mirrors data_enricher's per-row exception handling pattern.
                    logger.warning(
                        "[%d/%d] Per-job error for %s @ %s — skipping: %s",
                        i,
                        total,
                        title,
                        company,
                        exc,
                    )

        finally:
            browser.close()

    # DEFECT 008 FIX: guard division with `total or 1` so a future refactor that
    # removes the early-exit guard cannot cause ZeroDivisionError here.
    logger.info(
        "Agentic enrichment complete: %d/%d jobs enriched (%.0f%%)",
        enriched_count,
        total,
        100 * enriched_count / (total or 1),
    )
    return enriched_count
