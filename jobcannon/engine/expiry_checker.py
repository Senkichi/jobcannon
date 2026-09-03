# PORTED from job_finder/web/expiry_checker.py @ 039de5a24d4eceba3992c036cd33ecff1ca9592a (private job-cannon). Ledger L-0182.
"""Job expiry detection and unified staleness orchestrator.

Provides:
    _extract_posting_id   -- Extract individual posting ID from ATS URL
    _check_ats_api        -- Per-posting ATS API liveness check (Lever/GH/Ashby)
    _check_careers_page   -- Company careers page title-search signal
    _check_job_expiry     -- Signal cascade orchestrator for a single job
    quick_liveness_check  -- Lightweight HTTP GET check for a single URL
    check_job_liveness    -- Scoring preflight wrapper around quick_liveness_check
    run_staleness_check   -- Nightly unified orchestrator (B → A → C)

Architecture:
- Thread-safe: creates own sqlite3 connection (same pattern as stale_detector).
- Signal cascade (per job): URL GET → per-posting ATS API → careers-page search.
  SerpAPI was removed: absence from its index is a weak signal that caused false
  positives, and the per-job 30-second timeout dominated runtime.
- Unified orchestrator (run_staleness_check) runs three phases in order:
    Phase B: batch ATS reconciliation (ats_reconciler.reconcile_all_companies)
    Phase C: parallel HTTP cascade over jobs not yet resolved by Phase B
    Phase A: time-based stale marking (stale_detector.run_stale_detection)
  Order matters: B and C both refresh last_seen for verified-live jobs
  (B inline, C via persist_job_expiry_state's live path), so A — the only
  phase that acts on the clock instead of direct evidence — must run last,
  judging against the freshest evidence available.
"""

import copy
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Hashable
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError, as_completed
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import requests

# PORT-SEAM: db._direct_link.set_direct_url and db.{persist_job_expiry_state,
# update_pipeline_status} imports dropped -- svc.set_direct_url / svc.persist_job_expiry_state
# / svc.update_pipeline_status seams (see the block further below and the docstring
# on _persist_cascade_worker_result).
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_registry import (
    EXPIRY_CHECKER_POSTING_ID_PATTERNS,
    extract_greenhouse_posting_id,
)

# PORT-SEAM: db_helpers.standalone_connection (DIES) -> svc.connection_factory()
from jobcannon.engine.direct_link import _posting_link
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.opaque_redirect_candidates import (
    is_opaque_redirect_candidate,
    record_signal0_outcome,
)
from jobcannon.engine.services import get_services
from jobcannon.engine.source_registry import first_source_url, is_opaque_redirect_source
# PORT-SEAM: job_finder.web.db_helpers.standalone_connection is DIES ->
# svc.connection_factory(). job_finder.web.primary_source_tiebreak is L-0230
# (HOLD) -> svc.tiebreak_primary_posting(...). job_finder.db.{persist_job_
# expiry_state,update_pipeline_status} have no public counterpart and no
# ledger row in this port's read scope -> svc.persist_job_expiry_state(...) /
# svc.update_pipeline_status(...), same fallback rule as the named HOLD rows.

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMEOUT = 10  # seconds for HTTP requests inside the cascade

# Default python-requests UA gets bot-walled (challenge pages, 403/999) by
# LinkedIn, Workday, and most aggregators, inflating INCONCLUSIVE and
# false-LIVE counts. A browser UA keeps the checks on the normal page path.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Signal result constants
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

# Default concurrency for Phase C (configurable)
_DEFAULT_PARALLEL_WORKERS = 10
_MIN_CASCADE_PARALLEL_WORKERS = 1
# Ceiling matches the historical default of 10 (not the [1,6] ATS-platform
# bound used by Phase B): cascade fans out across DISTINCT company hosts, so
# per-host pacing (HOST_PACING_LIMIT in ats_platforms/_concurrency.py) already
# bounds any single host and 10 cross-host workers is real parallelism.
_MAX_CASCADE_PARALLEL_WORKERS = 10

# Greenhouse redirects to the board root with ?error=true when a posting is gone.
# (Merged from liveness_checker._GREENHOUSE_ERROR_RE.)
_GREENHOUSE_ERROR_RE = re.compile(r"[?&]error=true")


class LivenessResult(str):
    """A liveness verdict string that also carries the outcome context.

    Subclasses ``str`` so existing ``result == EXPIRED`` and
    ``persist_job_expiry_state(..., result, ...)`` callers keep working.
    The extra fields let the Signal-0 outcome accumulator distinguish a
    skipped check (opaque/shadow source) from an attempted check that was
    blocked by an auth wall.
    """

    def __new__(
        cls,
        value: str,
        *,
        status_code: int | None = None,
        blocked: bool = False,
        attempted: bool = True,
    ):
        obj = super().__new__(cls, value)
        obj.status_code = status_code
        obj.blocked = blocked
        obj.attempted = attempted
        return obj


class CascadeResult(NamedTuple):
    """Result of the signal cascade in _check_job_expiry.

    Fields:
        result: The cascade result (EXPIRED, LIVE, or INCONCLUSIVE).
        evidence: Human-readable evidence string describing how the result was reached.
        direct_url: Optional direct URL to the job posting on the company's careers page.
            Populated only on a confident Signal 2 match (strict keyword-boundary or
            LLM-assisted). Written by the caller via svc.set_direct_url on the orchestrator
            thread. (# PORT-SEAM: db._direct_link.set_direct_url seam)
        careers_attempted: True whenever Signal 2 actually executed. Used to stamp
            jobs.careers_checked_at — Plan 2 is the sole writer and Plan 3 the sole reader.
        signal0_attempted: True whenever Signal 0 (quick_liveness_check) actually ran.
        signal0_blocked: True when Signal 0 returned an auth/anti-bot block.
    """

    result: str
    evidence: str
    direct_url: str | None = None
    careers_attempted: bool = False
    signal0_attempted: bool = False
    signal0_blocked: bool = False


# ---------------------------------------------------------------------------
# Posting ID extraction
# ---------------------------------------------------------------------------


def _extract_posting_id(url: str, ats_platform: str) -> str | None:
    """Extract the individual posting ID from an ATS URL.

    Used by Signal 1 (per-posting ATS API). Greenhouse routes through the
    registry's multi-shape extractor (canonical/EU host, custom-domain gh_jid,
    embed token) — its single source of truth — so self-hosted Greenhouse boards
    (e.g. careers.airbnb.com/…?gh_jid=<id>) resolve. Lever and Ashby use their
    single dict pattern. Workday and SmartRecruiters don't expose equivalent
    single-posting endpoints; they rely on Phase B batch reconciliation via
    jobcannon.engine.ats_reconciler.
    """
    if ats_platform == "greenhouse":
        return extract_greenhouse_posting_id(url)
    pattern = EXPIRY_CHECKER_POSTING_ID_PATTERNS.get(ats_platform)
    if pattern is None:
        return None
    match = pattern.search(url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Signal 1: ATS API Check (per-posting)
# ---------------------------------------------------------------------------


def _check_ats_api(slug: str, posting_id: str, ats_platform: str, timeout: int = _TIMEOUT) -> str:
    """Check if a specific job posting is still live via the ATS API."""
    if ats_platform == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}"
    elif ats_platform == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}"
    elif ats_platform == "ashby":
        # Ashby's GraphQL API is complex; check the public job board URL instead
        url = f"https://jobs.ashbyhq.com/{slug}/{posting_id}"
    else:
        return INCONCLUSIVE

    try:
        resp = fetch_with_deadline(url, getter=requests.get, timeout=timeout, headers=_HEADERS)
        if resp.status_code in (404, 410):
            return EXPIRED
        if resp.status_code == 200:
            return LIVE
        return INCONCLUSIVE
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return INCONCLUSIVE
    except Exception as e:
        logger.warning("_check_ats_api: unexpected error for %s/%s: %s", slug, posting_id, e)
        return INCONCLUSIVE


# ---------------------------------------------------------------------------
# Signal 2: Company Careers Page Check
# ---------------------------------------------------------------------------

# PORT-SEAM: careers_scraper.find_careers_url / scrape_careers_page are
# ALREADY-existing ScanServices fields (svc.find_careers_url /
# svc.scrape_careers_page) -- see _check_careers_page below, and the
# identical precedent in jobcannon/engine/ats_scanner/_run_html.py.
try:
    from jobcannon.engine.ats_platforms import _title_matches
except ImportError:
    _title_matches = None  # type: ignore[assignment]


class CareersPageMemo:
    """Per-run, thread-safe memo for careers-page URL resolution and scrape
    results.

    Serves two independent cache domains from the same store/lock machinery,
    disambiguated by key shape: careers-URL resolution (``find_careers_url``)
    is keyed by a bare ``homepage_url`` string, scrape results
    (``scrape_careers_page``) by ``(careers_url, target_titles_hash)`` tuples.
    A ``str`` key can never equal a ``tuple`` key, so both domains coexist in
    the same store without namespace collisions or a second lock structure.

    The first caller for a key invokes the provided factory; subsequent
    callers for the same key block on a per-key lock and reuse the cached
    result. Exceptions are also cached so a failed resolution or scrape is
    not retried for the same key in the same run.
    """

    def __init__(self):
        self._store: dict[Hashable, object] = {}
        self._key_locks: dict[Hashable, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def get_or_compute(self, key: Hashable, factory: Callable[[], object]) -> tuple[object, bool]:
        """Return the cached value for *key*, creating it with *factory* if needed.

        Returns ``(value, is_fresh)`` where ``is_fresh`` is True iff the value
        was just computed by this call. If *factory* raises, the exception is
        cached and returned so subsequent callers see the same failure.
        """
        # Fast path: already cached
        if key in self._store:
            return self._store[key], False

        # Get or create a per-key lock serialized under _locks_lock
        with self._locks_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock

        with lock:
            if key in self._store:
                return self._store[key], False
            try:
                value = factory()
            except Exception as exc:
                self._store[key] = exc
                return exc, True
            self._store[key] = value
            return value, True


def _target_titles_hash(target_titles: list[str], exclusions: list[str]) -> int:
    """Hashable key for the search parameters passed to ``scrape_careers_page``.

    Includes both inclusion and exclusion title keywords because both affect the
    set of results the scraper returns. Normalized by lowercasing and sorting so
    equivalent keyword lists produce the same cache key.
    """
    return hash(
        (
            tuple(sorted(t.lower() for t in target_titles)),
            tuple(sorted(e.lower() for e in exclusions)),
        )
    )


def _check_careers_page(
    homepage_url: str | None,
    job_title: str,
    target_titles: list[str],
    exclusions: list[str],
    *,
    db_path: str | None = None,
    config: dict | None = None,
    job_id: str | None = None,
    careers_memo: CareersPageMemo | None = None,
) -> tuple[str, str | None, bool]:
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0182)
    """Check if a job title appears on the company's careers page.

    Returns (status, matched_url, attempted). attempted is True whenever this
    check actually ran a careers-page fetch (homepage_url present and the
    careers_scraper module available) — False only when the check never had
    a chance to start (job-listing-verification Plan 3 uses attempted to
    stamp jobs.careers_checked_at). matched_url is populated only on a LIVE
    verdict — the existing strict keyword-boundary match, or (when db_path
    and config are supplied) a confident LLM-assisted match.

    When ``careers_memo`` is provided, both the URL resolution
    (``find_careers_url``, keyed by ``homepage_url``) and the raw scrape
    result (``scrape_careers_page``, keyed by ``(careers_url,
    target_titles_hash)``) are shared across postings in the same run, so the
    company homepage and careers page are each fetched only once. Memoized
    failures (including exceptions) are also reused.
    """
    if not homepage_url:
        return INCONCLUSIVE, None, False

    if (
        svc.find_careers_url is None or svc.scrape_careers_page is None
    ):  # PORT-SEAM: careers_scraper seam
        logger.debug("_check_careers_page: careers_scraper not available")
        return INCONCLUSIVE, None, False

    try:
        if careers_memo is not None:
            cached_url, _ = careers_memo.get_or_compute(
                homepage_url,
                lambda: svc.find_careers_url(homepage_url),  # PORT-SEAM: careers_scraper seam
            )
            if isinstance(cached_url, BaseException):
                raise copy.copy(cached_url)
            careers_url = cached_url
        else:
            careers_url = svc.find_careers_url(homepage_url)  # PORT-SEAM: careers_scraper seam
        if not careers_url:
            return INCONCLUSIVE, None, True

        if careers_memo is not None:
            key = (careers_url, _target_titles_hash(target_titles, exclusions))
            cached, _ = careers_memo.get_or_compute(
                key,
                lambda: svc.scrape_careers_page(
                    careers_url, target_titles, exclusions
                ),  # PORT-SEAM: careers_scraper seam
            )
            if isinstance(cached, BaseException):
                raise copy.copy(cached)
            results, _ = cached
        else:
            results, _ = svc.scrape_careers_page(
                careers_url, target_titles, exclusions
            )  # PORT-SEAM: careers_scraper seam

        for item in results:
            result_title = item.get("title", "")
            if _title_matches is not None:
                if _title_matches(result_title, [job_title], []):
                    return LIVE, item.get("url"), True
            else:
                if (
                    job_title.lower() in result_title.lower()
                    or result_title.lower() in job_title.lower()
                ):
                    return LIVE, item.get("url"), True

        # LLM-assisted fallback (Section 3, job-listing-verification Plan 2):
        # the strict keyword-boundary match failed, but the scrape did
        # surface candidates — a freeform HTML title often doesn't literally
        # contain the job title as a phrase even when it's the same posting.
        # Reuses primary_source_tiebreak's exact proven pattern (same
        # forced-match safeguards: only a confident verdict counts). Opens
        # its own short-lived connection — this may run inside Phase C's
        # ThreadPoolExecutor, where the orchestrator's shared conn is unsafe
        # to touch from a worker thread (_cascade_worker's own docstring).
        if results and db_path and config is not None:
            try:
                if svc.tiebreak_primary_posting is None:  # PORT-SEAM: L-0230 HOLD
                    matched = None
                else:
                    with (
                        svc.connection_factory() as tiebreak_conn
                    ):  # PORT-SEAM: careers_scraper seam
                        matched = svc.tiebreak_primary_posting(
                            results,
                            job_title,
                            "",
                            None,
                            tiebreak_conn,
                            config,
                            job_id=job_id,
                        )
            except Exception as e:
                logger.warning(
                    "_check_careers_page: LLM tie-break unavailable for %s: %s",
                    homepage_url,
                    e,
                )
                matched = None
            if matched:
                return LIVE, _posting_link(matched), True

        return INCONCLUSIVE, None, True

    except Exception as e:
        logger.debug("_check_careers_page: error checking %s: %s", homepage_url, e)
        return INCONCLUSIVE, None, True


# ---------------------------------------------------------------------------
# In-memory careers-page failure tracker (Signal 2 backoff)
# ---------------------------------------------------------------------------

_careers_lock = threading.Lock()
_careers_failure_counts: dict[int, int] = {}
_careers_skip_until: dict[int, datetime] = {}

_MAX_CAREERS_FAILURES = 3
_CAREERS_SKIP_DAYS = 7


def _record_careers_outcome(company_id: int | None, success: bool) -> None:
    """Track careers-page check outcome for backoff logic."""
    if company_id is None:
        return
    with _careers_lock:
        if success:
            _careers_failure_counts.pop(company_id, None)
            _careers_skip_until.pop(company_id, None)
        else:
            count = _careers_failure_counts.get(company_id, 0) + 1
            _careers_failure_counts[company_id] = count
            if count >= _MAX_CAREERS_FAILURES:
                _careers_skip_until[company_id] = datetime.now(UTC) + timedelta(
                    days=_CAREERS_SKIP_DAYS
                )
                logger.info(
                    "_record_careers_outcome: company %d hit %d failures, skipping for %d days",
                    company_id,
                    count,
                    _CAREERS_SKIP_DAYS,
                )


# ---------------------------------------------------------------------------
# Signal 0: Direct URL liveness check
# ---------------------------------------------------------------------------

# Expired-page body markers. Lowercase — matched case-insensitively via body.lower().
# Merged from liveness_checker._EXPIRED_PATTERNS (the unique strings that
# weren't already here).
_EXPIRED_BODY_MARKERS = (
    "position filled",
    "position has been filled",
    "no longer accepting",
    "this job is no longer available",
    "job has been removed",
    "this job posting has been removed",
    "this position has been closed",
    "this job has expired",
    "this job posting has expired",
    "this listing has expired",
    "this job listing has expired",
    "this position is no longer open",
    "this role has been filled",
    "this position is no longer available",
    "job no longer available",
    "the position has been filled",
    "this job is closed",
    "this job has been closed",
    "sorry, this position has been filled",
    "sorry, this job has already been filled",
    "this opportunity is no longer available",
    # Merged from liveness_checker
    "the position you are looking for is no longer available",
    "this requisition is no longer active",
    "job not found",
    "posting not found",
    "this opening has been closed",
    # Greenhouse search-result page when board is empty
    "there are no jobs matching your search",
    # German
    "diese stelle ist nicht mehr verfügbar",
    "diese stelle ist nicht mehr verfugbar",
    "diese position wurde bereits besetzt",
    # French
    "cette offre n'est plus disponible",
    "cette offre n\u2019est plus disponible",
)

_EXPIRED_BODY_REGEXES = tuple(
    re.compile(p)
    for p in (
        r"this job\b.{0,50}\bis no longer available",
        r"this job\b.{0,30}\bis no longer accepting",
        r"no longer active",
        r"expired\s+on\s+\w+",
    )
)


def _auth_block_statuses(config: dict | None) -> set[int]:
    """Return the HTTP status codes treated as auth/anti-bot blocks."""
    return set(((config or {}).get("health") or {}).get("auth_block_statuses") or [401, 403, 429])


def quick_liveness_check(url: str, timeout: int = 8, config: dict | None = None) -> str:
    """Lightweight HTTP GET check for a single job URL.

    Used by the scoring preflight to gate score-tier evaluation AND by
    Phase C's cascade. Independent from the ATS-specific signals.

    Returns a LivenessResult: a string with ``blocked``, ``status_code`` and
    ``attempted`` attributes attached. ``blocked`` is True for auth/anti-bot
    statuses (401/403/429 by default) so callers can tally per-host outcomes.
    """
    # Greenhouse error-redirect URL — expired boards redirect to ?error=true
    if _GREENHOUSE_ERROR_RE.search(url):
        return LivenessResult(EXPIRED)

    block_statuses = _auth_block_statuses(config)
    try:
        resp = fetch_with_deadline(
            url, getter=requests.get, timeout=timeout, allow_redirects=True, headers=_HEADERS
        )
        if resp.status_code in (404, 410):
            return LivenessResult(EXPIRED)
        if resp.status_code == 200:
            body_lower = resp.text[:5000].lower()
            for marker in _EXPIRED_BODY_MARKERS:
                if marker in body_lower:
                    return LivenessResult(EXPIRED)
            for pattern in _EXPIRED_BODY_REGEXES:
                if pattern.search(body_lower):
                    return LivenessResult(EXPIRED)
            return LivenessResult(LIVE)
        blocked = resp.status_code in block_statuses
        return LivenessResult(
            INCONCLUSIVE,
            status_code=resp.status_code,
            blocked=blocked,
        )
    except requests.exceptions.HTTPError as e:
        status_code = getattr(e.response, "status_code", None)
        blocked = status_code in block_statuses if status_code is not None else False
        return LivenessResult(
            INCONCLUSIVE,
            status_code=status_code,
            blocked=blocked,
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return LivenessResult(INCONCLUSIVE)
    except Exception as e:
        logger.debug("quick_liveness_check: error for %s: %s", url, e)
        return LivenessResult(INCONCLUSIVE)


def check_job_liveness(job_row: dict, config: dict, conn: sqlite3.Connection | None = None) -> str:
    """Check if a job posting is still live by testing its first source URL.

    Note: This function implements the 2c skip for known opaque-redirect sources
    (e.g. Jooble) by returning INCONCLUSIVE without HTTP fetch, since these
    sources return 403 for all direct URL checks. The expiry checker cascade
    (_check_job_expiry) still falls through to Signal 1 (ATS API) and Signal 2
    (careers page) for these jobs.

    When *conn* is provided, the host of the first source URL is also checked
    against the derived opaque-redirect shadow list. This lets the scoring
    preflight benefit from hosts auto-flagged by Phase C without performing a
    redundant direct GET.
    """
    if is_opaque_redirect_source(job_row, config):
        return LivenessResult(INCONCLUSIVE, attempted=False)

    first_url = first_source_url(job_row)
    if not first_url:
        return LivenessResult(INCONCLUSIVE, attempted=False)

    if is_opaque_redirect_candidate(first_url, conn=conn, config=config):
        return LivenessResult(INCONCLUSIVE, attempted=False)

    timeout = (config.get("verification") or {}).get("liveness_check_timeout_s", 8)
    return quick_liveness_check(first_url, timeout=timeout, config=config)


# ---------------------------------------------------------------------------
# Signal cascade orchestrator (per job)
# ---------------------------------------------------------------------------


def _check_job_expiry(
    job: dict,
    company: dict | None,
    config: dict,
    skip_careers: bool = False,
    db_path: str | None = None,
    careers_memo: CareersPageMemo | None = None,
) -> CascadeResult:
    """Run the signal cascade for a single job.

    Signals in order: direct URL → per-posting ATS API → careers-page search.
    Short-circuits on first definitive answer. db_path is required only for
    Signal 2's LLM-assisted match (Section 3, Plan 2) — passed through to
    _check_careers_page, which opens its own connection since this may run
    inside Phase C's ThreadPoolExecutor. careers_memo lets Signal 2 share the
    same raw careers-page scrape across postings in a single run.
    """
    title = job.get("title", "")
    timeout = config.get("staleness", {}).get("cascade_request_timeout_seconds", 8)

    source_urls_raw = job.get("source_urls", "[]")
    if isinstance(source_urls_raw, str):
        try:
            source_urls = json.loads(source_urls_raw)
        except (json.JSONDecodeError, TypeError):
            source_urls = []
    else:
        source_urls = source_urls_raw or []

    # --- Signal 0: Direct URL liveness check ---
    # Skip entirely (Plan 1 fix 2c) when the job's provenance is entirely
    # within the opaque-redirect registry — same guaranteed-403 reasoning as
    # check_job_liveness. Also skip when the first URL's host is a derived
    # opaque-redirect candidate (shadow list populated by observed Signal-0
    # outcomes). Skipping Signal 0 is recall-neutral: Signal 1/2 still run.
    first_url = first_source_url(job)
    signal0_attempted = False
    signal0_blocked = False
    if first_url and not is_opaque_redirect_source(job, config):
        if is_opaque_redirect_candidate(first_url, db_path=db_path, config=config):
            url_result = LivenessResult(INCONCLUSIVE, attempted=False)
        else:
            url_result = quick_liveness_check(first_url, timeout=timeout, config=config)
            signal0_attempted = True
            signal0_blocked = getattr(url_result, "blocked", False)
        if url_result == EXPIRED:
            return CascadeResult(
                EXPIRED,
                "url_check expired_markers",
                None,
                False,
                signal0_attempted,
                signal0_blocked,
            )
        if url_result == LIVE:
            return CascadeResult(
                LIVE, "url_check 200_ok", None, False, signal0_attempted, signal0_blocked
            )
        # INCONCLUSIVE falls through to Signal 1

    # --- Signal 1: Per-posting ATS API Check ---
    if company and company.get("ats_platform") and company.get("ats_slug"):
        platform = company["ats_platform"]
        slug = company["ats_slug"]
        posting_id = None
        for url in source_urls:
            posting_id = _extract_posting_id(url, platform)
            if posting_id:
                break

        if posting_id:
            result = _check_ats_api(slug, posting_id, platform, timeout=timeout)
            if result == EXPIRED:
                return CascadeResult(
                    EXPIRED,
                    f"{platform}_api 404",
                    None,
                    False,
                    signal0_attempted,
                    signal0_blocked,
                )
            if result == LIVE:
                return CascadeResult(
                    LIVE,
                    f"{platform}_api 200",
                    None,
                    False,
                    signal0_attempted,
                    signal0_blocked,
                )

    # --- Signal 2: Careers Page Check ---
    if not skip_careers:
        homepage_url = company.get("homepage_url") if company else None
        target_titles = config.get("profile", {}).get("target_titles", [])
        exclusions = config.get("profile", {}).get("exclusions", {}).get("title_keywords", [])
        careers_result, careers_url, careers_attempted = _check_careers_page(
            homepage_url,
            title,
            target_titles,
            exclusions,
            db_path=db_path,
            config=config,
            job_id=job.get("dedup_key"),
            careers_memo=careers_memo,
        )
        if careers_result == LIVE:
            return CascadeResult(
                LIVE,
                "careers_page title_found",
                careers_url,
                careers_attempted,
                signal0_attempted,
                signal0_blocked,
            )
        return CascadeResult(
            INCONCLUSIVE,
            "",
            None,
            careers_attempted,
            signal0_attempted,
            signal0_blocked,
        )

    return CascadeResult(INCONCLUSIVE, "", None, False, signal0_attempted, signal0_blocked)


# ---------------------------------------------------------------------------
# Parallel cascade worker
# ---------------------------------------------------------------------------


def _cascade_worker(
    job: dict,
    company: dict | None,
    config: dict,
    db_path: str,
    careers_memo: CareersPageMemo | None = None,
) -> tuple[str, str, str, str | None, bool, bool, bool]:
    """Execute the cascade for one job in a ThreadPoolExecutor worker.

    Returns (dedup_key, result, evidence, direct_url, careers_attempted,
    signal0_attempted, signal0_blocked).
    Worker path is fully read-only against the shared orchestrator DB
    connection; all writes happen on the orchestrator thread. db_path lets
    Signal 2's LLM-assisted match (_check_careers_page) open its own,
    separate connection when it needs one — never the orchestrator's shared
    connection, which is unsafe to touch from a worker thread.
    """
    dedup_key = job["dedup_key"]
    company_id = company.get("id") if company else None

    # Check careers-page failure backoff (Signal 2 only)
    skip_careers = False
    with _careers_lock:
        skip_until = _careers_skip_until.get(company_id) if company_id else None
    if skip_until and datetime.now(UTC) < skip_until:
        skip_careers = True

    try:
        cascade = _check_job_expiry(
            job,
            company,
            config,
            skip_careers=skip_careers,
            db_path=db_path,
            careers_memo=careers_memo,
        )
    except Exception as e:
        logger.warning("cascade_worker: error checking %s: %s", dedup_key, e)
        return (
            dedup_key,
            INCONCLUSIVE,
            f"worker_error:{type(e).__name__}",
            None,
            False,
            False,
            False,
        )

    return (
        dedup_key,
        cascade.result,
        cascade.evidence,
        cascade.direct_url,
        cascade.careers_attempted,
        cascade.signal0_attempted,
        cascade.signal0_blocked,
    )


# PORT-SEAM: db._direct_link.set_direct_url is an OPTIONAL ScanServices
# seam (svc.set_direct_url), not a direct import -- its Postgres-native
# %s SQL cannot run against the bare sqlite3 connections tests/engine/
# uses, even though it is safe against a real connection_factory
# connection in production (internal conn.raw unwrap). See
# _persist_cascade_worker_result below.


def _persist_cascade_worker_result(
    conn: sqlite3.Connection,
    job: dict,
    company: dict | None,
    worker_result: tuple[str, str, str, str | None, bool, bool, bool],
    summary: dict,
    _company_outcomes: dict[int, bool],
    config: dict,
) -> None:
    """Persist one worker's tuple into the shared DB connection and update counters.

    Runs on the orchestrator thread so all writes are serialized across workers.
    """
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0182)
    (
        dedup_key,
        verdict,
        evidence,
        direct_url,
        careers_attempted,
        signal0_attempted,
        signal0_blocked,
    ) = worker_result

    if signal0_attempted:
        first_url = first_source_url(job)
        record_signal0_outcome(conn, first_url, signal0_attempted, signal0_blocked, config)

    now = utc_now_iso()

    if (
        direct_url and svc.set_direct_url is not None
    ):  # PORT-SEAM: db._direct_link.set_direct_url seam
        svc.set_direct_url(conn, dedup_key, direct_url, "strict")

    if careers_attempted:
        conn.execute(
            "UPDATE jobs SET careers_checked_at = ? WHERE dedup_key = ?",
            (now, dedup_key),
        )

    # PORT-SEAM: persist_job_expiry_state / update_pipeline_status have no
    # public counterpart and no ledger row in this port's read scope (L-0182).
    if verdict == EXPIRED:
        if svc.persist_job_expiry_state is not None:  # PORT-SEAM: db._persistence seam
            svc.persist_job_expiry_state(conn, dedup_key, EXPIRED, now)
        if svc.update_pipeline_status is not None:
            svc.update_pipeline_status(
                conn,
                dedup_key,
                "archived",
                source="expiry_check",
                evidence=evidence,
            )
        summary["archived"] += 1
        logger.info("_run_phase_c_cascade: archived %s (%s)", dedup_key, evidence)
    elif verdict == LIVE:
        if svc.persist_job_expiry_state is not None:  # PORT-SEAM: db._persistence seam
            svc.persist_job_expiry_state(conn, dedup_key, LIVE, now)
        summary["live"] += 1
    else:
        if svc.persist_job_expiry_state is not None:  # PORT-SEAM: db._persistence seam
            svc.persist_job_expiry_state(conn, dedup_key, INCONCLUSIVE, now)
        summary["inconclusive"] += 1

    company_id = company.get("id") if company else None
    homepage_url = company.get("homepage_url") if company else None
    if company_id and homepage_url and careers_attempted:
        if "careers_page" in evidence:
            _company_outcomes[company_id] = True
        elif verdict == INCONCLUSIVE and _company_outcomes.get(company_id) is None:
            _company_outcomes[company_id] = False


# ---------------------------------------------------------------------------
# Phase C: parallel HTTP cascade
# ---------------------------------------------------------------------------


def _get_cascade_parallel_workers(staleness_cfg: dict) -> int:
    """Read Phase C cascade concurrency from config, clamped to a sane range.

    Mirrors ats_reconciler._get_batch_ats_parallel_workers's clamp pattern
    (issue #1032 review finding): floored at 1 so operators can always force
    strictly-sequential fallback. Unlike Phase B's [1, 6] bound — which
    matches the per-platform board-scan ceiling in ats_platforms/_concurrency.py
    — Phase C fans out across DISTINCT company hosts, where per-host pacing
    (HOST_PACING_LIMIT) already bounds any single host, so the ceiling here
    matches Phase C's historically-validated default of 10 instead.
    """
    try:
        max_workers = int(staleness_cfg.get("cascade_parallel_workers", _DEFAULT_PARALLEL_WORKERS))
    except (TypeError, ValueError):
        max_workers = _DEFAULT_PARALLEL_WORKERS
    return max(_MIN_CASCADE_PARALLEL_WORKERS, min(_MAX_CASCADE_PARALLEL_WORKERS, max_workers))


def _get_cascade_runtime_limit_s(staleness_cfg: dict) -> float | None:
    """Read Phase C cascade runtime limit from config, clamped to non-negative.

    0 or absent means no limit.
    """
    try:
        runtime_limit_s = float(staleness_cfg.get("cascade_runtime_limit_s", 0))
    except (TypeError, ValueError):
        runtime_limit_s = 0
    runtime_limit_s = max(0.0, runtime_limit_s)
    return runtime_limit_s if runtime_limit_s > 0 else None


def _run_phase_c_cascade(db_path: str, config: dict) -> dict:
    """Parallel cascade for jobs not yet resolved by Phase B.

    Workers run HTTP + regex only; writes happen on the main thread.
    """
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0182)
    staleness_cfg = config.get("staleness", {})
    legacy_expiry_cfg = config.get("expiry", {})

    recheck_days = staleness_cfg.get(
        "cascade_recheck_days",
        legacy_expiry_cfg.get("recheck_days", 3),
    )
    max_workers = _get_cascade_parallel_workers(staleness_cfg)

    runtime_limit_s = _get_cascade_runtime_limit_s(staleness_cfg)

    summary = {"checked": 0, "archived": 0, "live": 0, "inconclusive": 0}

    with svc.connection_factory() as conn:  # PORT-SEAM: ScanServices seam
        recheck_cutoff = (datetime.now(UTC) - timedelta(days=recheck_days)).isoformat()
        rows = conn.execute(
            """
            SELECT j.*, c.ats_platform, c.ats_slug, c.homepage_url, c.id AS company_row_id
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE j.pipeline_status IN ('discovered', 'reviewing')
              AND (j.expiry_status IS NULL OR j.expiry_status != 'expired')
              AND (j.expiry_checked_at IS NULL OR j.expiry_checked_at < ?)
            ORDER BY j.expiry_checked_at IS NULL DESC, j.expiry_checked_at ASC
            """,
            (recheck_cutoff,),
        ).fetchall()

        summary["checked"] = len(rows)
        if not rows:
            logger.info("_run_phase_c_cascade: no jobs to check")
            return summary

        # Build work items: (job_dict, company_dict_or_none)
        work_items: list[tuple[dict, dict | None]] = []
        for row in rows:
            job = dict(row)
            company: dict | None = None
            if job.get("ats_platform"):
                company = {
                    "ats_platform": job["ats_platform"],
                    "ats_slug": job["ats_slug"],
                    "homepage_url": job.get("homepage_url"),
                    "id": job.get("company_row_id"),
                }
            elif job.get("homepage_url"):
                company = {
                    "homepage_url": job["homepage_url"],
                    "ats_platform": None,
                    "ats_slug": None,
                    "id": job.get("company_row_id"),
                }
            work_items.append((job, company))

        logger.info(
            "_run_phase_c_cascade: %d jobs, %d workers",
            len(work_items),
            max_workers,
        )

        careers_memo = CareersPageMemo()
        _company_outcomes: dict[int, bool] = {}

        processed = 0
        start_monotonic = time.monotonic()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(_cascade_worker, job, company, config, db_path, careers_memo): (
                    job,
                    company,
                )
                for job, company in work_items
            }

            completed_futures: set = set()
            for future in as_completed(future_to_item):
                processed += 1
                job, company = future_to_item[future]
                completed_futures.add(future)
                try:
                    worker_result = future.result()
                except Exception as e:
                    logger.warning(
                        "_run_phase_c_cascade: worker future failed for %s: %s",
                        job.get("dedup_key"),
                        e,
                    )
                    summary["inconclusive"] += 1
                else:
                    _persist_cascade_worker_result(
                        conn, job, company, worker_result, summary, _company_outcomes, config
                    )

                if (
                    runtime_limit_s is not None
                    and time.monotonic() - start_monotonic >= runtime_limit_s
                ):
                    logger.info(
                        "_run_phase_c_cascade: runtime limit %.0fs reached, stopped after %d/%d jobs",
                        runtime_limit_s,
                        processed,
                        summary["checked"],
                    )
                    summary["truncated"] = True
                    break

            # Drain already-running futures so the work we've already paid for is
            # not discarded. Cancel pending ones first so the executor doesn't
            # start more work while we wait for the stragglers. Running ones are
            # awaited with a bounded per-future timeout and persisted like the
            # main loop.
            if summary.get("truncated"):
                per_request_timeout = max(
                    1.0, (config.get("staleness") or {}).get("cascade_request_timeout_seconds", 8)
                )
                per_future_timeout = max(1.0, runtime_limit_s + per_request_timeout)
                remaining_futures = [f for f in future_to_item if f not in completed_futures]
                for future in remaining_futures:
                    future.cancel()
                for future in remaining_futures:
                    # Skip only cancelled futures (no result to keep). A future
                    # that is already done still holds a ready result the main
                    # loop never yielded — .result() returns it instantly and it
                    # must be persisted like any other drained straggler.
                    if future.cancelled():
                        continue
                    job, company = future_to_item[future]
                    try:
                        worker_result = future.result(timeout=per_future_timeout)
                    except (CancelledError, TimeoutError):
                        logger.warning(
                            "_run_phase_c_cascade: worker future drained/cancelled for %s",
                            job.get("dedup_key"),
                        )
                    except Exception as e:
                        logger.warning(
                            "_run_phase_c_cascade: worker future failed for %s: %s",
                            job.get("dedup_key"),
                            e,
                        )
                        summary["inconclusive"] += 1
                    else:
                        _persist_cascade_worker_result(
                            conn, job, company, worker_result, summary, _company_outcomes, config
                        )

        # Update the cross-run careers-page backoff ledger once per run per
        # company. This preserves the 3-strike behavior while keeping one
        # memoized failure from counting as 278 strikes in a single run.
        for company_id, success in _company_outcomes.items():
            _record_careers_outcome(company_id, success)

    logger.info("_run_phase_c_cascade complete: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Public API: unified staleness orchestrator
# ---------------------------------------------------------------------------


def run_staleness_check(db_path: str, config: dict) -> dict:
    """Unified nightly staleness orchestrator.

    Runs three phases in order:
        Phase B: batch ATS reconciliation (one HTTP call per company)
        Phase C: parallel HTTP cascade for jobs not resolved by Phase B
        Phase A: time-based stale marking + passive-stage archive

    Order matters: Phases B and C both refresh last_seen for verified-live
    jobs (B inline, C via persist_job_expiry_state). Phase A is the only
    phase that infers from the clock rather than direct evidence, so it
    runs last — a job HTTP-verified live tonight must not be stale-marked
    or clock-archived tonight.
    """
    staleness_cfg = config.get("staleness", {})
    if not staleness_cfg.get("enabled", True):
        legacy_expiry_cfg = config.get("expiry", {})
        if not legacy_expiry_cfg.get("enabled", True):
            logger.info("run_staleness_check: disabled via config")
            return {
                "phase_b": {},
                "phase_a": {},
                "phase_c": {},
                "disabled": True,
            }

    summary: dict = {"phase_b": {}, "phase_a": {}, "phase_c": {}}

    # --- Phase B: batch ATS reconciliation ---
    svc_b = get_services()  # PORT-SEAM: ScanServices seam (L-0182)
    if staleness_cfg.get("batch_ats_enabled", True) and svc_b.reconcile_all_companies is not None:
        try:
            summary["phase_b"] = svc_b.reconcile_all_companies(
                db_path, config
            )  # PORT-SEAM: L-0135 ADAPT
        except Exception:
            logger.exception("run_staleness_check: Phase B failed")
            summary["phase_b"] = {"error": True}
    elif not svc_b.reconcile_all_companies:  # PORT-SEAM: L-0135 ADAPT, unlanded
        logger.info(
            "run_staleness_check: Phase B skipped -- ats_reconciler not wired "
            "(L-0135, no ScanServices.reconcile_all_companies host callable)"
        )
    else:
        logger.info("run_staleness_check: Phase B disabled via config")

    # --- Phase C: parallel HTTP cascade ---
    try:
        summary["phase_c"] = _run_phase_c_cascade(db_path, config)
    except Exception:
        logger.exception("run_staleness_check: Phase C failed")
        summary["phase_c"] = {"error": True}

    # --- Phase A: time-based stale / archive (last: judges on the evidence
    # B and C just refreshed) ---
    try:
        from jobcannon.engine.stale_detector import run_stale_detection

        summary["phase_a"] = run_stale_detection(db_path, config)
    except Exception:
        logger.exception("run_staleness_check: Phase A failed")
        summary["phase_a"] = {"error": True}

    logger.info("run_staleness_check complete: %s", summary)
    return summary
