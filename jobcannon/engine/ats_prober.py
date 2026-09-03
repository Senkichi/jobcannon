# PORTED from job_finder/web/ats_prober.py @ b24cf4a6b434f96154144ee087acbae766b4e255 (private job-cannon). Ledger L-0016.
"""Single-company ATS probing with retry, backoff, and error handling."""

import dataclasses
import logging
import sqlite3
import time  # noqa: F401 — available for callers that may need it
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_detection import derive_slug_candidates, extract_ats_from_url_best
from jobcannon.engine.brand_blocklist import is_blocked_brand
from jobcannon.engine.http_fetch import fetch_with_deadline

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8  # seconds

# PORT-SEAM (L-0016): Host-injectable extension bundle (None-default seam).
# Replaces the private source's lazy imports of ats_identity_reconcile /
# ats_slug_challenge / careers_crawler tiers — none of which port to the
# engine (see ledger rows L-0013 / L-0022, both ADAPT, whose seam is
# ScanServices, not standalone engine modules). Duck-typed: any object
# exposing the nine callables below (same signatures as the private
# functions they stand in for). With no bundle registered, the static-first
# fall-through records a miss ("static_fallthrough_unavailable") and
# speculative slug promotion FAILS CLOSED: claims are stamped provisional and
# a slug collision never demotes the incumbent — promotion without identity
# verification is how phantom-company pollution happens. The scan-orchestration
# entry point (jobcannon.engine.ats_scanner._run.run_ats_scan) propagates
# services.prober_extensions into set_prober_extensions() automatically
# (restoring the prior value in a finally) — see services.py's module
# docstring for the full wiring.
#   promote_from_careers_link, identity_reconcile_settings,
#   owner_identity_passes, resolve_slug_collision,
#   new_summary, try_static_extract, try_embedded_json_extract,
#   try_playwright_extract, upsert_and_log
_prober_extensions: Any | None = None


def set_prober_extensions(ext: Any | None) -> None:
    """Register (or clear, with None) the host's prober extension bundle."""
    global _prober_extensions
    _prober_extensions = ext


# Probe status precedence for upsert conflict resolution (higher = more advanced)
_PROBE_STATUS_PRECEDENCE = {
    "hit": 2,
    "pending": 1,
    "miss": 0,
}

# ---------------------------------------------------------------------------
# Retry state machine constants (DEBT-01 / Phase 14)
# ---------------------------------------------------------------------------

# Backoff schedule: [1hr, 4hr, 24hr] — index = current retry_count before increment
_BACKOFF_HOURS = [1, 4, 24]
_MAX_RETRIES = 3  # After 3 consecutive failures → permanent unreachable miss

# HTTP status codes that indicate transient failures (retry eligible)
_TRANSIENT_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# HTTP status codes that indicate permanent miss (no retry)
_PERMANENT_MISS_CODES: frozenset[int] = frozenset({404, 410})


@dataclasses.dataclass(frozen=True)
class ProbeHttpResult:
    """Outcome of a raising-variant HTTP probe (``_probe_*_raising`` /
    ``_probe_lever_with_result``): whether the response counted as a hit,
    plus the raw status code.

    The raising variants used to collapse straight to ``bool``
    (``status_code == 200``), which meant any non-200 response that didn't
    *raise* (429, 5xx, 401, 403, ...) silently became ``False`` —
    indistinguishable from a genuine 404. Surfacing ``status_code`` lets
    :func:`jobcannon.engine.ats_registry.verify_live_detail` run it through the
    same :func:`_is_transient_error` chokepoint used for exceptions, instead
    of maintaining a second, status-blind notion of transience.
    """

    hit: bool
    status_code: int


# ---------------------------------------------------------------------------
# Retry state machine helpers (DEBT-01 / Phase 14)
# ---------------------------------------------------------------------------


def _compute_retry_after(retry_count: int) -> str:
    """Compute UTC ISO timestamp for next retry based on current retry_count.

    Uses _BACKOFF_HOURS schedule: [1hr, 4hr, 24hr].
    retry_count is the count BEFORE the current failure (before incrementing).

    Returns timestamps in SQLite datetime() format ("YYYY-MM-DD HH:MM:SS") so that
    comparisons like retry_after < datetime('now') work correctly in SQL queries.

    Args:
        retry_count: Current retry_count value (0-based index into backoff schedule).

    Returns:
        UTC timestamp string in SQLite-compatible format for SQL datetime comparisons.
    """
    index = min(retry_count, len(_BACKOFF_HOURS) - 1)
    hours = _BACKOFF_HOURS[index]
    dt = datetime.now(UTC) + timedelta(hours=hours)
    # Return in SQLite-compatible UTC format (no timezone offset suffix) for
    # correct comparison with datetime('now') in SQL WHERE clauses
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_transient_error(exc_or_status) -> bool:
    """Return True if the given exception or status code indicates a transient error.

    Args:
        exc_or_status: Either an exception instance or an integer HTTP status code.

    Returns:
        True if the error is transient (should retry), False if permanent.
    """
    if isinstance(exc_or_status, int):
        return exc_or_status in _TRANSIENT_CODES
    # Check for requests exception types indicating transient network issues
    return isinstance(
        exc_or_status,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    )


def _handle_scan_error(
    conn: sqlite3.Connection,
    company_id: int,
    company_name: str,
    error_detail: str,
    now: str,
) -> None:
    """Handle a transient ATS scan/probe error for a company.

    Reads current retry_count from companies table. If retry_count >= _MAX_RETRIES - 1
    (i.e. already had max retries), promotes to permanent miss with miss_reason='unreachable'.
    Otherwise, increments retry_count and sets retry_after using exponential backoff.

    Args:
        conn: Open SQLite connection.
        company_id: Company row ID.
        company_name: Company name (for logging).
        error_detail: Description of the error.
        now: Current UTC ISO timestamp string.
    """
    row = conn.execute("SELECT retry_count FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        logger.warning("_handle_scan_error: company %d not found", company_id)
        return

    current_retry_count = row[0] or 0

    if current_retry_count >= _MAX_RETRIES - 1:
        # 3rd consecutive failure → promote to permanent unreachable miss
        new_retry_count = _MAX_RETRIES
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'miss',
                   miss_reason = 'unreachable',
                   retry_count = ?,
                   updated_at = ?
               WHERE id = ?""",
            (new_retry_count, now, company_id),
        )
        conn.commit()
        logger.info(
            "_handle_scan_error: %s promoted to unreachable after %d failures",
            company_name,
            new_retry_count,
        )
    else:
        # Transient error — increment retry_count, set backoff retry_after
        new_retry_count = current_retry_count + 1
        retry_after = _compute_retry_after(current_retry_count)
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'error',
                   retry_count = ?,
                   retry_after = ?,
                   updated_at = ?
               WHERE id = ?""",
            (new_retry_count, retry_after, now, company_id),
        )
        conn.commit()
        logger.info(
            "_handle_scan_error: %s set to error (retry %d/%d), retry_after=%s. Error: %s",
            company_name,
            new_retry_count,
            _MAX_RETRIES,
            retry_after,
            error_detail,
        )


def _reset_retry_state(
    conn: sqlite3.Connection,
    company_id: int,
    now: str,
) -> None:
    """Reset retry state after a successful probe/scan.

    Sets retry_count=0, retry_after=NULL, miss_reason=NULL on the company row.
    Does NOT change ats_probe_status — caller is responsible for setting that.

    Args:
        conn: Open SQLite connection.
        company_id: Company row ID.
        now: Current UTC ISO timestamp string.
    """
    conn.execute(
        """UPDATE companies
           SET retry_count = 0,
               retry_after = NULL,
               miss_reason = NULL,
               updated_at = ?
           WHERE id = ?""",
        (now, company_id),
    )
    conn.commit()


def _try_static_first_fallthrough(
    company_id: int,
    company_name: str,
    careers_url: str,
    conn: sqlite3.Connection,
    config: dict,
    now: str,
) -> dict:
    """Static-first fall-through for companies without a known ATS platform.

    Implements the cheap→expensive ordering per issue #565:
    1. Re-detect known ATS on subdomain (careers_url + discovered links)
    2. Static HTML extract (L1/L4 from careers_crawler)
    3. Embedded-JSON tier (Tier 2.5)
    4. Playwright tier (if static extraction signals JS-heavy)

    On success, promotes the company to the detected ATS or persists jobs from
    custom careers pages. Custom pages are NOT marked as 'hit' (that state
    requires a real ATS platform with platform+slug); instead they are marked
    as 'miss' with scan_enabled=TRUE and jobs persisted, so the careers_crawler
    picks them up for ongoing extraction. Sets specific miss_reason on failure.

    Args:
        company_id: Company row ID.
        company_name: Company name (for logging).
        careers_url: The company's careers URL.
        conn: Open SQLite connection.
        config: Application config dict.
        now: Current UTC ISO timestamp string.

    Returns:
        Dict with "status" key: "hit" (promoted to ATS), "miss" (custom page
        with jobs persisted, or all tiers failed), or "error" (transient failure).
    """
    from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT

    ext = _prober_extensions
    if ext is None:
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'miss',
                   miss_reason = ?,
                   updated_at = ?
               WHERE id = ?""",
            ("static_fallthrough_unavailable", now, company_id),
        )
        conn.commit()
        logger.info(
            "static_fallthrough: no prober extensions registered for %s — miss",
            company_name,
        )
        return {"status": "miss", "reason": "static_fallthrough_unavailable"}

    # Extract target titles and exclusions from config for title filtering
    profile_cfg = config.get("profile", {})
    target_titles = profile_cfg.get("target_titles", [])
    exclusions_cfg = profile_cfg.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    # Get DB path for job persistence (needed by _upsert_and_log)
    db_path = config.get("DB_PATH")

    # Fetch company row for conditional logic (Fix 3 and Fix 4)
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    # Track Tier 1 outcome for meaningful non-promoted results (Fix 4)
    tier1_outcome = None

    # ------------------------------------------------------------
    # Tier 1: Re-detect known ATS on subdomain
    # ------------------------------------------------------------
    logger.debug("static_fallthrough: tier1 re-detect ATS for %s", company_name)
    try:
        # Check the careers_url itself
        ats_hit = extract_ats_from_url_best(careers_url)
        if ats_hit:
            platform, slug, _ = ats_hit
            logger.info(
                "static_fallthrough: detected ATS on careers_url %s -> %s/%s",
                careers_url,
                platform,
                slug[:48],
            )
            # Compute reenable_scan based on company state (Fix 3)
            # Only re-enable for the m074 cohort: no known platform, prior miss
            reenable = company["ats_platform"] is None and company["ats_probe_status"] == "miss"
            res = ext.promote_from_careers_link(
                conn,
                company_id,
                platform,
                slug,
                page_url=careers_url,
                config=config,
                reenable_scan=reenable,
            )
            if res.get("outcome") == "promoted":
                return {"status": "hit", "source": "ats_redetect_careers_url"}
            else:
                # Surface non-promoted outcomes (Finding 5, Fix 4)
                tier1_outcome = res.get("outcome", "unknown")
                logger.debug(
                    "static_fallthrough: tier1 ATS detection on careers_url returned non-promoted outcome: %s",
                    tier1_outcome,
                )
                # Continue to next tier - this is a custom page, not a detected ATS
                pass

        # Fetch the careers page to discover subdomain links
        try:
            resp = fetch_with_deadline(
                careers_url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS
            )
            if resp.status_code == 200:
                html = resp.text
                soup = BeautifulSoup(html, "html.parser")
                # Extract all links from the page
                for tag in soup.find_all("a", href=True):
                    href = tag["href"].strip()
                    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                        continue
                    # Resolve relative URLs
                    if href.startswith("/"):
                        from urllib.parse import urljoin

                        href = urljoin(careers_url, href)
                    # Check if this link points to a known ATS
                    ats_hit = extract_ats_from_url_best(href)
                    if ats_hit:
                        platform, slug, _ = ats_hit
                        logger.info(
                            "static_fallthrough: detected ATS on subdomain link %s -> %s/%s",
                            href,
                            platform,
                            slug[:48],
                        )
                        # Compute reenable_scan based on company state (Fix 3)
                        # Only re-enable for the m074 cohort: no known platform, prior miss
                        reenable = (
                            company["ats_platform"] is None
                            and company["ats_probe_status"] == "miss"
                        )
                        res = ext.promote_from_careers_link(
                            conn,
                            company_id,
                            platform,
                            slug,
                            page_url=href,
                            config=config,
                            reenable_scan=reenable,
                        )
                        if res.get("outcome") == "promoted":
                            return {"status": "hit", "source": "ats_redetect_subdomain"}
                        else:
                            # Surface non-promoted outcomes (Finding 5, Fix 4)
                            tier1_outcome = res.get("outcome", "unknown")
                            logger.debug(
                                "static_fallthrough: tier1 ATS detection on subdomain returned non-promoted outcome: %s",
                                tier1_outcome,
                            )
                            # Continue to next tier - this is a custom page, not a detected ATS
                            pass
        except Exception as e:
            logger.debug("static_fallthrough: tier1 fetch failed for %s: %s", company_name, e)
    except Exception as e:
        logger.debug("static_fallthrough: tier1 failed for %s: %s", company_name, e)

    # ------------------------------------------------------------
    # Tier 2: Static HTML extract (L1/L4)
    # ------------------------------------------------------------
    logger.debug("static_fallthrough: tier2 static extract for %s", company_name)
    try:
        static_jobs = ext.try_static_extract(careers_url, target_titles, title_exclusions)
        if static_jobs is not None:
            # static_jobs is a list (may be empty) -> page was statically rendered
            if len(static_jobs) > 0:
                # Found jobs statically -> persist them and enable scan for custom careers page
                # This is NOT an ATS 'hit' (no platform+slug), so we mark as 'miss' with
                # scan_enabled=TRUE and a specific miss_reason, so careers_crawler picks it up.
                if db_path:
                    summary = ext.new_summary()
                    all_new_job_keys = []
                    ext.upsert_and_log(
                        static_jobs,
                        company_id,
                        company_name,
                        now,
                        db_path,
                        summary,
                        all_new_job_keys,
                        "static_fallthrough_tier2",
                    )
                    logger.info(
                        "static_fallthrough: tier2 persisted %d jobs for %s (custom careers)",
                        len(static_jobs),
                        company_name,
                    )
                else:
                    logger.warning(
                        "static_fallthrough: tier2 found %d jobs but no DB_PATH configured, cannot persist",
                        len(static_jobs),
                    )

                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           scan_enabled = TRUE,
                           miss_reason = 'static_fallthrough_tier2_jobs_persisted',
                           updated_at = ?
                       WHERE id = ?""",
                    (now, company_id),
                )
                conn.commit()
                return {
                    "status": "miss",
                    "reason": "static_fallthrough_tier2_jobs_persisted",
                    "jobs_found": len(static_jobs),
                }
            else:
                # Statically rendered but no matching jobs -> genuinely empty
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'static_fallthrough_tier2_no_matches',
                           updated_at = ?
                       WHERE id = ?""",
                    (now, company_id),
                )
                conn.commit()
                logger.debug("static_fallthrough: tier2 no matches for %s", company_name)
                return {"status": "miss", "reason": "static_fallthrough_tier2_no_matches"}
        # static_jobs is None -> page appears JS-heavy, continue to next tier
    except Exception as e:
        logger.debug("static_fallthrough: tier2 failed for %s: %s", company_name, e)
        # Surface exception-specific miss_reason (Finding 5)
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'miss',
                   miss_reason = 'static_fallthrough_tier2_exception',
                   updated_at = ?
               WHERE id = ?""",
            (now, company_id),
        )
        conn.commit()
        return {"status": "miss", "reason": "static_fallthrough_tier2_exception"}

    # ------------------------------------------------------------
    # Tier 3: Embedded-JSON extraction (Tier 2.5)
    # ------------------------------------------------------------
    logger.debug("static_fallthrough: tier3 embedded JSON for %s", company_name)
    try:
        json_jobs = ext.try_embedded_json_extract(careers_url, target_titles, title_exclusions)
        if json_jobs is not None:
            # json_jobs is a list (may be empty) -> embedded JSON found
            if len(json_jobs) > 0:
                # Found jobs in embedded JSON -> persist them and enable scan for custom careers page
                # This is NOT an ATS 'hit' (no platform+slug), so we mark as 'miss' with
                # scan_enabled=TRUE and a specific miss_reason, so careers_crawler picks it up.
                if db_path:
                    summary = ext.new_summary()
                    all_new_job_keys = []
                    ext.upsert_and_log(
                        json_jobs,
                        company_id,
                        company_name,
                        now,
                        db_path,
                        summary,
                        all_new_job_keys,
                        "static_fallthrough_tier3",
                    )
                    logger.info(
                        "static_fallthrough: tier3 persisted %d jobs for %s (embedded JSON)",
                        len(json_jobs),
                        company_name,
                    )
                else:
                    logger.warning(
                        "static_fallthrough: tier3 found %d jobs but no DB_PATH configured, cannot persist",
                        len(json_jobs),
                    )

                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           scan_enabled = TRUE,
                           miss_reason = 'static_fallthrough_tier3_jobs_persisted',
                           updated_at = ?
                       WHERE id = ?""",
                    (now, company_id),
                )
                conn.commit()
                return {
                    "status": "miss",
                    "reason": "static_fallthrough_tier3_jobs_persisted",
                    "jobs_found": len(json_jobs),
                }
            else:
                # Embedded JSON found but no matching jobs
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'static_fallthrough_tier3_no_matches',
                           updated_at = ?
                       WHERE id = ?""",
                    (now, company_id),
                )
                conn.commit()
                logger.debug("static_fallthrough: tier3 no matches for %s", company_name)
                return {"status": "miss", "reason": "static_fallthrough_tier3_no_matches"}
        # json_jobs is None -> no embedded JSON found, continue to next tier
    except Exception as e:
        logger.debug("static_fallthrough: tier3 failed for %s: %s", company_name, e)
        # Surface exception-specific miss_reason (Finding 5)
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'miss',
                   miss_reason = 'static_fallthrough_tier3_exception',
                   updated_at = ?
               WHERE id = ?""",
            (now, company_id),
        )
        conn.commit()
        return {"status": "miss", "reason": "static_fallthrough_tier3_exception"}

    # ------------------------------------------------------------
    # Tier 4: Playwright (most expensive - only if earlier tiers failed)
    # ------------------------------------------------------------
    logger.debug("static_fallthrough: tier4 Playwright for %s", company_name)
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            # Playwright not installed - skip this tier
            logger.debug("static_fallthrough: Playwright not installed, skipping tier4")
        else:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    playwright_jobs = ext.try_playwright_extract(
                        browser, careers_url, target_titles, title_exclusions
                    )
                    if len(playwright_jobs) > 0:
                        # Found jobs via Playwright -> persist them and enable scan for custom careers page
                        # This is NOT an ATS 'hit' (no platform+slug), so we mark as 'miss' with
                        # scan_enabled=TRUE and a specific miss_reason, so careers_crawler picks it up.
                        if db_path:
                            summary = ext.new_summary()
                            all_new_job_keys = []
                            ext.upsert_and_log(
                                playwright_jobs,
                                company_id,
                                company_name,
                                now,
                                db_path,
                                summary,
                                all_new_job_keys,
                                "static_fallthrough_tier4",
                            )
                            logger.info(
                                "static_fallthrough: tier4 persisted %d jobs for %s (Playwright)",
                                len(playwright_jobs),
                                company_name,
                            )
                        else:
                            logger.warning(
                                "static_fallthrough: tier4 found %d jobs but no DB_PATH configured, cannot persist",
                                len(playwright_jobs),
                            )

                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'miss',
                                   scan_enabled = TRUE,
                                   miss_reason = 'static_fallthrough_tier4_jobs_persisted',
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, company_id),
                        )
                        conn.commit()
                        return {
                            "status": "miss",
                            "reason": "static_fallthrough_tier4_jobs_persisted",
                            "jobs_found": len(playwright_jobs),
                        }
                    else:
                        # Playwright rendered but no matching jobs
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'miss',
                                   miss_reason = 'static_fallthrough_tier4_no_matches',
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, company_id),
                        )
                        conn.commit()
                        logger.debug("static_fallthrough: tier4 no matches for %s", company_name)
                        return {"status": "miss", "reason": "static_fallthrough_tier4_no_matches"}
                finally:
                    browser.close()
    except Exception as e:
        logger.debug("static_fallthrough: tier4 failed for %s: %s", company_name, e)
        # Surface exception-specific miss_reason (Finding 5)
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'miss',
                   miss_reason = 'static_fallthrough_tier4_exception',
                   updated_at = ?
               WHERE id = ?""",
            (now, company_id),
        )
        conn.commit()
        return {"status": "miss", "reason": "static_fallthrough_tier4_exception"}

    # ------------------------------------------------------------
    # All tiers failed
    # ------------------------------------------------------------
    # Fold meaningful Tier 1 outcomes into miss_reason (Fix 4)
    # Meaningful outcomes: slug_collision, abstain_conflict, disabled
    # Not meaningful: "no ATS candidate found at all" (tiers 2-4 have their own reasons)
    meaningful_outcomes = {"slug_collision", "abstain_conflict", "disabled"}
    if tier1_outcome in meaningful_outcomes:
        final_reason = f"static_fallthrough_exhausted_tier1_{tier1_outcome}"
    else:
        final_reason = "static_fallthrough_all_tiers_exhausted"

    conn.execute(
        """UPDATE companies
           SET ats_probe_status = 'miss',
               miss_reason = ?,
               updated_at = ?
           WHERE id = ?""",
        (final_reason, now, company_id),
    )
    conn.commit()
    logger.info(
        "static_fallthrough: all tiers exhausted for %s (reason: %s)", company_name, final_reason
    )
    return {"status": "miss", "reason": final_reason}


def _promote_speculative_hit(
    conn: sqlite3.Connection,
    company_id: int,
    company_name: str,
    platform: str,
    slug: str,
    config: dict,
) -> bool:
    """Write a speculative-ladder hit, demoting a poisoned owner via the
    slug-ownership challenge mechanism when the m076 UNIQUE(ats_platform,
    ats_slug) constraint collides.

    A speculative name-derived guess is lower-confidence than reconcile's
    job-URL evidence (the whole reason bamboohr/personio/recruitee/breezy were
    pulled from this ladder — see ats_scanner._probe's _FP_PRONE_PLATFORMS
    docstring), so a single collision must NOT immediately evict an
    incumbent. ``resolve_slug_collision`` applies the same identity
    re-verification and consecutive-challenge threshold used by every other
    promotion write site, so a poisoned owner can still be demoted here —
    just not by one speculative guess alone. The identity-challenge machinery
    (``owner_identity_passes`` / ``resolve_slug_collision`` /
    ``identity_reconcile_settings``) lives host-side and arrives via the
    ``_prober_extensions`` bundle (see ``set_prober_extensions``); with no
    bundle registered this function fails closed (see below).

    Like every other promotion write site, this claim is scored with
    ``owner_identity_passes`` and stamped ``ats_evidence_provisional`` (see
    ats_slug_challenge's module docstring, "First-acquisition gap") — this
    ladder has no evidence trigger (always NULL here), so the score falls
    through to name-vs-slug affinity, which the candidate was itself DERIVED
    from (``derive_slug_candidates``), so this is non-provisional in the
    common case; only a genuine name/slug divergence marks it.

    Returns True once the (platform, slug, 'hit') UPDATE lands for
    ``company_id`` (first try or after a demotion); False if the pair stays
    with its current owner and the caller should try the next candidate.
    """
    own = conn.execute(
        "SELECT name, name_raw FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    own_name = own["name"] if own else ""
    own_name_raw = own["name_raw"] if own else ""
    ext = _prober_extensions
    is_provisional = (
        0
        if (ext is not None and ext.owner_identity_passes(own_name, own_name_raw, None, slug))
        else 1
    )

    # Every column below is nulled explicitly (not just omitted) so a company
    # row that once held an evidence-based promotion — then got its slug
    # cleared by a path that doesn't touch these columns (companies.py's
    # update-slug route, company_dedup's heal) — can't re-enter the
    # speculative ladder and keep a stale non-NULL ats_evidence_trigger. The
    # NULL-means-speculative invariant (relied on by
    # m064_reset_fp_prone_speculative_hits) must hold regardless of the row's
    # prior state, not just on a freshly-inserted row.
    speculative_sql = """UPDATE companies
               SET ats_probe_status = 'hit',
                   ats_platform = ?,
                   ats_slug = ?,
                   ats_evidence_trigger = NULL,
                   ats_evidence_extractor_version = NULL,
                   ats_evidence_unique_url_count = NULL,
                   ats_evidence_job_count = NULL,
                   ats_evidence_reconciled_at = NULL,
                   ats_evidence_provisional = ?,
                   consecutive_empty_scans = 0
               WHERE id = ?"""
    try:
        conn.execute(speculative_sql, (platform, slug, is_provisional, company_id))
        return True
    except sqlite3.IntegrityError as ie:
        if ext is None:
            # Fail closed: without the slug-ownership challenge machinery a
            # single speculative guess must never evict an incumbent owner.
            return False

        collision = ext.resolve_slug_collision(
            conn,
            platform=platform,
            slug=slug,
            challenger_id=company_id,
            settings=ext.identity_reconcile_settings(config),
            config=config,
        )
        if collision["demoted"]:
            try:
                conn.execute(speculative_sql, (platform, slug, is_provisional, company_id))
            except sqlite3.IntegrityError:
                # Unreachable by construction (this transaction just cleared
                # the owner's claim) unless another writer raced in between;
                # don't ride an unrelated commit on this uncommitted demotion.
                conn.rollback()
                return False
            return True
        if collision["challenge"] and collision["challenge"]["recorded"]:
            # Persist challenge bookkeeping even though promotion is refused.
            conn.commit()
        logger.warning(
            "probe_single_company: collision %s/%s for %s — leaving existing ats_slug. exc=%s",
            platform,
            slug,
            company_name,
            ie,
        )
        return False


def probe_single_company(
    company_id: int,
    conn: sqlite3.Connection,
    config: dict,
) -> dict:
    """Probe a single company's ATS platform and update its state.

    Used by the manual retry route (POST /companies/<id>/retry) to immediately
    re-probe a company in error or unreachable state.

    Uses the caller's conn (Flask request thread g.db) — NOT its own connection.
    This differs from probe_ats_slugs/run_ats_scan which create their own connections.

    Args:
        company_id: The companies table row ID.
        conn: Open SQLite connection (caller's — Flask g.db or test conn).
        config: Application config dict (reads TESTING flag).

    Returns:
        Dict with at minimum a "status" key: "hit", "error", or "miss".
        "hit" also includes "jobs_found". "error" includes "detail".
    """
    now = utc_now_iso()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if company is None:
        return {"status": "miss", "detail": "company not found"}

    platform = company["ats_platform"]
    slug = company["ats_slug"]
    company_name = company["name_raw"]

    # If company has a known platform and slug, probe directly via the
    # registry-driven dispatch (#1928). The former hand-maintained if/elif
    # chain over 9 platforms omitted phenom / oracle_cloud / ultipro (and
    # others) even though their _probe_* functions were implemented — so no
    # code path could move those companies to 'hit'. Resolving the probe via
    # ats_registry.PLATFORMS.get(platform).probe_attr makes every PlatformSpec
    # with a probe reachable, and the completeness test in
    # test_ats_registry_completeness.py pins dispatcher coverage == registry
    # coverage so the next platform addition can't silently drift.
    #
    # verify_live_detail restores the transient/blocked-vs-permanent
    # distinctions that the original #1928 refactor collapsed: for platforms
    # with a probe_raising_attr (lever/greenhouse/ashby/smartrecruiters — the
    # four that had inline retry-aware HTTP pre-#1928), a Timeout/
    # ConnectionError OR a 429/5xx status is classified as TRANSIENT and
    # routed through _handle_scan_error (retry-with-backoff); a non-transient,
    # non-404/410 status (401/403 — the slug exists but the probe was denied)
    # is classified as BLOCKED and recorded as a distinct platform_slug_blocked
    # miss, instead of either collapsing into a permanent platform_slug_404
    # miss. Platforms without a raising variant fall back to the bool
    # verify_live (HIT or MISS only) — matching their pre-#1928
    # swallowing-probe behaviour.
    if platform and slug:
        from jobcannon.engine import ats_registry

        spec = ats_registry.PLATFORMS.get(platform)
        if spec is not None and spec.probe_attr is not None:
            try:
                outcome = ats_registry.verify_live_detail(platform, slug)
            except Exception as e:
                # Non-transient exception from a raising-variant probe (e.g.
                # JSONDecodeError from a malformed response). verify_live_detail
                # catches Timeout/ConnectionError itself (→ TRANSIENT), so this
                # except only sees non-transient failures — treat them as a
                # generic retryable error, same as the pre-#1928 broad except.
                logger.warning("probe_single_company: %s unexpected error: %s", company_name, e)
                _handle_scan_error(conn, company_id, company_name, str(e), now)
                return {"status": "error", "detail": str(e)}
            if outcome is ats_registry.ProbeOutcome.HIT:
                conn.execute(
                    "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
                    (company_id,),
                )
                _reset_retry_state(conn, company_id, now)
                logger.info("probe_single_company: %s -> hit (%s)", company_name, platform)
                return {"status": "hit", "jobs_found": 0}
            if outcome is ats_registry.ProbeOutcome.TRANSIENT:
                # Transient network failure (timeout / connection error / 429 /
                # 5xx on a raising-variant platform). Route through the retry-
                # with-backoff state machine instead of permanently recording
                # a platform_slug_404 miss — restores the pre-#1928 behaviour
                # for lever/greenhouse/ashby/smartrecruiters that the registry
                # dispatch collapsed (#1928 rework review).
                detail = f"transient probe failure for {platform}/{slug}"
                _handle_scan_error(conn, company_id, company_name, detail, now)
                return {"status": "error", "detail": detail}
            if outcome is ats_registry.ProbeOutcome.BLOCKED:
                # Probe reached a real response but got a non-transient,
                # non-404/410 status (401/403) — the slug exists but access
                # was denied, as distinct from a slug that doesn't resolve at
                # all. Restores the pre-#1928 platform_slug_blocked diagnostic
                # distinction (deleted by the #1928 refactor) so audits can
                # tell "doesn't exist" apart from "exists but blocked" (#1928
                # rework review fold-in). Not retried automatically — an
                # operator can still hit Retry via is_company_retryable's
                # probeable-slug clause.
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'platform_slug_blocked'
                       WHERE id = ?""",
                    (company_id,),
                )
                conn.commit()
                logger.info(
                    "probe_single_company: %s -> blocked (%s/%s)", company_name, platform, slug
                )
                return {"status": "miss", "reason": "platform_slug_blocked"}
            # outcome is MISS — slug doesn't resolve to a live board.
            # Try static-first fallthrough if careers_url exists (company
            # may have migrated ATS), same as the pre-#1928 404/410 path.
            careers_url = company["careers_url"]
            if careers_url:
                logger.info(
                    "probe_single_company: %s slug 404 for %s, trying static-first fallthrough",
                    platform,
                    company_name,
                )
                return _try_static_first_fallthrough(
                    company_id, company_name, careers_url, conn, config, now
                )
            else:
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'platform_slug_404'
                       WHERE id = ?""",
                    (company_id,),
                )
                conn.commit()
                return {"status": "miss"}
        else:
            # Platform value is set but has no registered probe (keyword
            # adapter, non_scannable stub, or unknown platform). Try
            # static-first fallthrough if careers_url exists.
            careers_url = company["careers_url"]
            if careers_url:
                logger.info(
                    "probe_single_company: unknown platform %s for %s, trying static-first fallthrough",
                    platform,
                    company_name,
                )
                return _try_static_first_fallthrough(
                    company_id, company_name, careers_url, conn, config, now
                )
            else:
                return {
                    "status": "miss",
                    "detail": f"unknown platform: {platform}",
                    "miss_reason": "unknown_platform",
                }

    else:
        # No platform/slug — try speculative probing via derived slug candidates.
        # F8: short-circuit famous-brand names (Shopify, Walmart, ...) — the
        # speculative ladder produces ~29% FPs on these because slug-collisions
        # with small-company ATS tenants are common. See brand_blocklist.py.
        if is_blocked_brand(company_name):
            logger.info("probe_single_company: %s blocked by brand blocklist", company_name)
            # Try static-first fallthrough if careers_url exists (brand blocklist only applies to speculative probing)
            careers_url = company["careers_url"]
            if careers_url:
                logger.info(
                    "probe_single_company: brand-blocked %s has careers_url, trying static-first fallthrough",
                    company_name,
                )
                return _try_static_first_fallthrough(
                    company_id, company_name, careers_url, conn, config, now
                )
            else:
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status='miss', miss_reason='blocked_brand'
                       WHERE id=?""",
                    (company_id,),
                )
                conn.commit()
                return {"status": "miss", "detail": "blocked_brand"}
        candidates = derive_slug_candidates(company_name)
        for slug_candidate in candidates:
            try:
                if _probe_lever_with_result(slug_candidate).hit:
                    if not _promote_speculative_hit(
                        conn, company_id, company_name, "lever", slug_candidate, config
                    ):
                        continue
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
                if _probe_greenhouse(slug_candidate):
                    if not _promote_speculative_hit(
                        conn, company_id, company_name, "greenhouse", slug_candidate, config
                    ):
                        continue
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
                if _probe_ashby(slug_candidate):
                    if not _promote_speculative_hit(
                        conn, company_id, company_name, "ashby", slug_candidate, config
                    ):
                        continue
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                _handle_scan_error(conn, company_id, company_name, str(e), now)
                return {"status": "error", "detail": str(e)}

        # All candidates exhausted — try static-first fallthrough if careers_url exists
        careers_url = company["careers_url"]
        if careers_url:
            logger.info(
                "probe_single_company: speculative probing failed for %s, trying static-first fallthrough",
                company_name,
            )
            return _try_static_first_fallthrough(
                company_id, company_name, careers_url, conn, config, now
            )
        else:
            # No careers_url — permanent miss with specific reason
            conn.execute(
                """UPDATE companies
                   SET ats_probe_status = 'miss',
                       miss_reason = 'speculative_probing_exhausted_no_careers_url'
                   WHERE id = ?""",
                (company_id,),
            )
            conn.commit()
            return {"status": "miss", "reason": "speculative_probing_exhausted_no_careers_url"}


def _probe_lever_with_result(slug: str) -> ProbeHttpResult:
    """Hit if Lever slug has at least one active posting; carries status_code
    for non-hit classification (see :class:`ProbeHttpResult`). Let transient
    exceptions propagate."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, timeout=_PROBE_TIMEOUT)
    hit = False
    if r.status_code == 200:
        data = r.json()
        hit = isinstance(data, list) and len(data) > 0
    return ProbeHttpResult(hit=hit, status_code=r.status_code)


def _probe_lever(slug: str) -> bool:
    """Return True if slug has at least one active Lever posting.

    IMPORTANT (Research Pitfall 2): Lever returns HTTP 200 with empty list
    for invalid slugs AND for valid slugs with no current postings. Only
    cache as 'hit' when response is 200 AND list has at least one posting.

    Args:
        slug: Lever company slug to probe.

    Returns:
        True if the slug is confirmed active on Lever (non-empty postings list).
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            # Per Research Pitfall 2: empty list is NOT a confirmed hit
            return isinstance(data, list) and len(data) > 0
        return False
    except Exception as e:
        logger.debug("_probe_lever('%s') failed: %s", slug, e)
        return False


def _probe_greenhouse(slug: str) -> bool:
    """Return True if slug is a valid Greenhouse board token.

    Greenhouse returns 200 for valid board tokens. 404 for invalid ones.

    Args:
        slug: Greenhouse board token to probe.

    Returns:
        True if the slug resolves to a valid Greenhouse job board.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_greenhouse('%s') failed: %s", slug, e)
        return False


def _probe_greenhouse_raising(slug: str) -> ProbeHttpResult:
    """Raising variant of :func:`_probe_greenhouse` for retry-aware dispatch.

    Identical HTTP logic but WITHOUT the broad ``except Exception`` — lets
    ``requests.exceptions.Timeout`` / ``ConnectionError`` propagate, and
    returns a :class:`ProbeHttpResult` (not a bare bool) so
    :func:`jobcannon.engine.ats_registry.verify_live_detail` can classify BOTH
    the raised exceptions and the response status code through the single
    ``_is_transient_error`` chokepoint: a status-code transient (429/5xx) or a
    Timeout/ConnectionError both become ``ProbeOutcome.TRANSIENT``, and
    ``probe_single_company`` routes that through ``_handle_scan_error``
    (retry-with-backoff) instead of permanently recording a
    ``platform_slug_404`` miss.

    Pre-#1928 ``probe_single_company`` did this HTTP call inline (not via the
    swallowing ``_probe_greenhouse``) for exactly this reason; the registry
    dispatch collapsed the distinction. This raising variant restores it
    without breaking the batch callers that depend on ``_probe_greenhouse``'s
    never-raise contract.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(url, timeout=_PROBE_TIMEOUT)
    return ProbeHttpResult(hit=r.status_code == 200, status_code=r.status_code)


def _probe_identity_greenhouse(slug: str) -> str | None:
    """Return the board's own display name from the Greenhouse Job Board API.

    ``GET /v1/boards/{slug}`` returns ``{"name": "...", "content": "..."}`` —
    the company's board display name, independent of the slug string itself
    (confirmed live: ``.../v1/boards/mercury`` -> ``{"name": "Mercury", ...}``).
    Used by ``ats_slug_challenge`` to break identity ties when two name-affine
    companies collide on one greenhouse slug (see its module docstring). This
    is a tie-break signal, never a liveness check — ``verify_live`` already
    owns that. Returns None on any non-200 response, malformed body, or error.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        name = data.get("name") if isinstance(data, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception as e:
        logger.debug("_probe_identity_greenhouse('%s') failed: %s", slug, e)
        return None


def _probe_workday(slug: str) -> bool:
    """Return True if Workday slug has active job postings.

    Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal").
    Parses subdomain to derive tenant (prefix before ".wd"), then POSTs to
    the standardized Workday CXS jobs API.

    Args:
        slug: Workday slug in "subdomain/board" format.

    Returns:
        True if the API returns 200 with jobPostings data.
    """
    parts = slug.split("/", 1)
    if len(parts) != 2:
        return False
    subdomain, board = parts

    # Derive tenant from subdomain: everything before ".wd" (e.g. "walmart" from "walmart.wd5")
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    try:
        r = requests.post(
            url,
            json={"limit": 1, "offset": 0, "searchText": ""},
            headers={"Content-Type": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_workday('%s') failed: %s", slug, e)
        return False


def _probe_oracle_cloud(slug: str) -> bool:
    """Return True if slug resolves to a live Oracle Recruiting Cloud (Fusion CE) site.

    Slug format: ``"{host}|{site}"`` (e.g. ``"ehmk.fa.us2.oraclecloud.com|CX_1"``).
    GETs the public Candidate-Experience REST finder for a single requisition; a
    200 means the pod + site resolve. Oracle's finder uses literal ``;,=``
    delimiters, so the query string is built by hand (mirrors the scanner's
    ``_fetch_postings`` — ``params=`` would percent-encode and break the finder).
    """
    host, _, site = (slug or "").partition("|")
    host = host.strip()
    site = site.strip() or "CX_1"
    if not host:
        return False
    url = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&finder=findReqs;siteNumber={site},limit=1,offset=0"
    )
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_oracle_cloud('%s') failed: %s", slug, e)
        return False


def _probe_ultipro(slug: str) -> bool:
    """Return True if slug resolves to a live UKG Pro Recruiting (UltiPro) board.

    Slug format: ``"{host}/{tenant}/{board}"`` (e.g.
    ``"recruiting2.ultipro.com/JAN1000JANI/<board-guid>"``). POSTs the public
    ``LoadSearchResults`` search endpoint with a minimal ``Top=1`` body; a 200
    means the board GUID resolves (empty boards still return 200).
    """
    parts = (slug or "").split("/")
    if len(parts) < 3 or not all(parts[:3]):
        return False
    host, tenant, board = parts[0], parts[1], parts[2]
    url = f"https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults"
    body = {
        "opportunitySearch": {
            "Top": 1,
            "Skip": 0,
            "QueryString": "",
            "OrderBy": [],
            "Filters": [],
        },
        "matchCriteria": {
            "PreferredJobs": [],
            "Educations": [],
            "LicenseAndCertifications": [],
            "Skills": [],
            "WorkExperiences": [],
            "DegreeFlexFields": [],
            "IsCurrentlyEmployed": False,
            "IsWillingToRelocate": False,
            "IsWillingToTravel": False,
            "EmploymentDesiredFlexFields": [],
        },
    }
    try:
        r = requests.post(
            url, json=body, headers={"Content-Type": "application/json"}, timeout=_PROBE_TIMEOUT
        )
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_ultipro('%s') failed: %s", slug, e)
        return False


def _probe_ibm(slug: str) -> bool:
    """Return True if IBM careers API is live.

    IBM is single-tenant — slug is ignored (constant "ibm"). POSTs the public
    search API with a minimal payload; a 200 means the API resolves (empty
    results still return 200).
    """
    url = "https://www-api.ibm.com/search/api/v2"
    body = {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {"bool": {"must": []}},
        "size": 1,
        "from": 0,
        "_source": [
            "field_text_01",
            "title",
            "field_keyword_05",
            "field_keyword_08",
            "field_keyword_19",
        ],
    }
    try:
        r = requests.post(
            url, json=body, headers={"Content-Type": "application/json"}, timeout=_PROBE_TIMEOUT
        )
        if r.status_code != 200:
            return False
        payload = r.json()
        hits = payload.get("hits", {}).get("hits", [])
        return len(hits) > 0
    except Exception as e:
        logger.debug("_probe_ibm('%s') failed: %s", slug, e)
        return False


def _probe_icims(slug: str) -> bool:
    """Return True if slug resolves to a live iCIMS career portal.

    iCIMS boards are 100% JS-rendered with no public unauthenticated JSON API
    (issue #454), so the probe only confirms the board *exists*: an HTTP GET
    of the portal's ``/jobs/search`` page returning 200 with an iCIMS marker
    in the body. The full JS render + job extraction is the Playwright
    scanner's job (``ats_platforms/_platforms_icims.py``) — keeping the probe
    requests-light avoids paying a browser launch just to confirm liveness.

    Tries the ``careers-`` host first, then ``jobs-`` (both prefixes are in
    active use across tenants). ``allow_redirects=True`` so tenants whose
    portal redirects to a branded subpath still register as live.

    Args:
        slug: iCIMS tenant subdomain (e.g. 'acme' for careers-acme.icims.com).

    Returns:
        True if the slug resolves to a live iCIMS portal.
    """
    for prefix in ("careers", "jobs"):
        url = f"https://{prefix}-{slug}.icims.com/jobs/search"
        try:
            r = requests.get(url, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        except Exception as e:
            logger.debug("_probe_icims('%s', prefix=%s) failed: %s", slug, prefix, e)
            continue
        if r.status_code == 200 and "icims" in r.text.lower():
            return True
    return False


def _probe_smartrecruiters(slug: str) -> bool:
    """Return True if SmartRecruiters company has active job postings.

    SmartRecruiters exposes a public Posting API (no auth required).
    Returns 200 with {"totalFound": N, "content": [...]} for valid companies.

    API: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1

    Args:
        slug: SmartRecruiters company identifier (e.g. 'LinkedIn3', 'AbbVie').

    Returns:
        True if the slug resolves to a company with active postings.
    """
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
    try:
        r = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("totalFound", 0) > 0
        return False
    except Exception as e:
        logger.debug("_probe_smartrecruiters('%s') failed: %s", slug, e)
        return False


def _probe_smartrecruiters_raising(slug: str) -> ProbeHttpResult:
    """Raising variant of :func:`_probe_smartrecruiters` for retry-aware dispatch.

    Lets ``requests.exceptions.Timeout`` / ``ConnectionError`` propagate and
    returns a :class:`ProbeHttpResult` (not a bare bool) so
    :func:`jobcannon.engine.ats_registry.verify_live_detail` can classify BOTH
    the raised exceptions and the response status code through the single
    ``_is_transient_error`` chokepoint. See :func:`_probe_greenhouse_raising`
    for the full rationale — same pattern, restoring the pre-#1928
    inline-HTTP retry semantics that the registry dispatch collapsed.
    """
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
    r = requests.get(
        url,
        headers={"Accept": "application/json"},
        timeout=_PROBE_TIMEOUT,
    )
    hit = False
    if r.status_code == 200:
        data = r.json()
        hit = data.get("totalFound", 0) > 0
    return ProbeHttpResult(hit=hit, status_code=r.status_code)


def _probe_identity_smartrecruiters(slug: str) -> str | None:
    """Return the company's own display name from the SmartRecruiters Postings API.

    SmartRecruiters has no standalone company-profile endpoint that resolves
    (``GET /v1/companies/{slug}`` alone 404s); every posting under
    ``GET /v1/companies/{slug}/postings`` embeds ``company.name`` — the same
    real display name for every posting on the board — so a 1-result fetch is
    enough (confirmed live against ``AbbVie``). Same tie-break role as
    ``_probe_identity_greenhouse`` — see ``ats_slug_challenge`` module
    docstring. Returns None on any non-200 response, empty result, malformed
    body, or error.
    """
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
    try:
        r = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, list) or not content:
            return None
        first = content[0]
        company = first.get("company") if isinstance(first, dict) else None
        name = company.get("name") if isinstance(company, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception as e:
        logger.debug("_probe_identity_smartrecruiters('%s') failed: %s", slug, e)
        return None


def _probe_ashby(slug: str) -> bool:
    """Return True if slug is a valid Ashby job board name.

    Note: Ashby slugs are case-sensitive (Research Pitfall 3).
    When probing from company name, the slug is lowercased. If this fails,
    the URL-derived slug (with original casing) should be used instead.

    Args:
        slug: Ashby job board name to probe.

    Returns:
        True if the slug resolves to a valid Ashby job board.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_ashby('%s') failed: %s", slug, e)
        return False


def _probe_ashby_raising(slug: str) -> ProbeHttpResult:
    """Raising variant of :func:`_probe_ashby` for retry-aware dispatch.

    Lets ``requests.exceptions.Timeout`` / ``ConnectionError`` propagate and
    returns a :class:`ProbeHttpResult` (not a bare bool) so
    :func:`jobcannon.engine.ats_registry.verify_live_detail` can classify BOTH
    the raised exceptions and the response status code through the single
    ``_is_transient_error`` chokepoint. See :func:`_probe_greenhouse_raising`
    for the full rationale.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(url, timeout=_PROBE_TIMEOUT)
    return ProbeHttpResult(hit=r.status_code == 200, status_code=r.status_code)


def _probe_recruitee(slug: str) -> bool:
    """Return True if slug has at least one active Recruitee offer.

    Recruitee may return 200 with an empty offers list for an inactive
    company (analogous to Lever's Research Pitfall 2), so the probe only
    confirms 'hit' on non-empty offers.

    Args:
        slug: Recruitee subdomain (e.g. 'acme' for acme.recruitee.com).

    Returns:
        True if the slug resolves to a Recruitee company with active offers.
    """
    url = f"https://{slug}.recruitee.com/api/offers/"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        offers = data.get("offers") if isinstance(data, dict) else None
        return isinstance(offers, list) and len(offers) > 0
    except Exception as e:
        logger.debug("_probe_recruitee('%s') failed: %s", slug, e)
        return False


def _probe_breezy(slug: str) -> bool:
    """Return True if slug has at least one active Breezy posting.

    Breezy returns 200 with an empty list for valid-but-empty tenants
    (same pitfall pattern as Lever/Recruitee), so the probe requires
    a non-empty list to confirm 'hit'.

    Args:
        slug: Breezy subdomain (e.g. 'acme' for acme.breezy.hr).

    Returns:
        True if the slug resolves to a Breezy company with active positions.
    """
    url = f"https://{slug}.breezy.hr/json"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if isinstance(data, list):
            return len(data) > 0
        if isinstance(data, dict):
            positions = data.get("positions") or data.get("jobs") or []
            return isinstance(positions, list) and len(positions) > 0
        return False
    except Exception as e:
        logger.debug("_probe_breezy('%s') failed: %s", slug, e)
        return False


def _probe_jazzhr(slug: str) -> bool:
    """Return True if slug has at least one active JazzHR posting.

    Same empty-list pitfall pattern — non-empty list required for 'hit'.

    Args:
        slug: JazzHR subdomain (e.g. 'acme' for acme.applytojob.com).

    Returns:
        True if the slug resolves to a JazzHR tenant with active jobs.
    """
    url = f"https://{slug}.applytojob.com/apply/jobs/feed"
    try:
        r = requests.get(url, params={"json": "1"}, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if isinstance(data, list):
            return len(data) > 0
        if isinstance(data, dict):
            jobs = data.get("jobs") or []
            return isinstance(jobs, list) and len(jobs) > 0
        return False
    except Exception as e:
        logger.debug("_probe_jazzhr('%s') failed: %s", slug, e)
        return False


def _probe_phenom(slug: str) -> bool:
    """Return True if Phenom slug has a valid sitemap with job URLs.

    Phenom does not expose a public JSON API. The probe checks if the
    sitemap index exists and contains at least one sitemap with job URLs.
    Uses the locale-aware sitemap discovery from the scanner module.

    Args:
        slug: Phenom careers host (e.g. 'careers.conduent.com').

    Returns:
        True if the slug resolves to a valid Phenom site with job listings.
    """
    from bs4 import BeautifulSoup

    from jobcannon.engine.ats_platforms._platforms_phenom import _sitemap_index_url

    try:
        sitemap_index_url = _sitemap_index_url(slug)
        r = requests.get(sitemap_index_url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False

        soup = BeautifulSoup(r.text, "xml")
        sitemap_locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]

        # Check first sitemap for job URLs
        for sitemap_url in sitemap_locs[:3]:  # Check first 3 sitemaps
            try:
                sr = requests.get(sitemap_url, timeout=_PROBE_TIMEOUT)
                if sr.status_code == 200:
                    ssoup = BeautifulSoup(sr.text, "xml")
                    job_locs = [loc.get_text(strip=True) for loc in ssoup.find_all("loc")]
                    if any("/job/" in url for url in job_locs):
                        return True
            except Exception:
                continue

        return False
    except Exception as e:
        logger.debug("_probe_phenom('%s') failed: %s", slug, e)
        return False


def _probe_pinpoint(slug: str) -> bool:
    """Return True if slug has at least one active Pinpoint posting.

    Pinpoint may return 200 with ``{"data": []}`` for tenants without active
    postings — empty-list pitfall, same as Lever/Recruitee.

    Args:
        slug: Pinpoint subdomain (e.g. 'workwithus' for workwithus.pinpointhq.com).

    Returns:
        True if the slug resolves to a Pinpoint tenant with active postings.
    """
    url = f"https://{slug}.pinpointhq.com/postings.json"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        postings = data.get("data") or []
        return isinstance(postings, list) and len(postings) > 0
    except Exception as e:
        logger.debug("_probe_pinpoint('%s') failed: %s", slug, e)
        return False


def _probe_personio(slug: str) -> bool:
    """Return True if slug has at least one active Personio position.

    Personio publishes XML at .de OR .com; this probe tries .de first then
    falls back to .com on 404. A valid feed with at least one <position> is
    a hit; empty <workzag-jobs> stays a miss (same pitfall pattern).

    Args:
        slug: Personio subdomain (e.g. 'acme' for acme.jobs.personio.de).

    Returns:
        True if the slug resolves to a Personio tenant with active positions.
    """
    for tld in ("de", "com"):
        url = f"https://{slug}.jobs.personio.{tld}/xml"
        try:
            r = requests.get(url, timeout=_PROBE_TIMEOUT)
        except Exception as e:
            logger.debug("_probe_personio('%s', tld=%s) failed: %s", slug, tld, e)
            continue
        if r.status_code == 404:
            continue
        if r.status_code != 200 or not r.content:
            continue
        # Parse cheaply — any <position> element is enough to confirm a hit.
        try:
            import defusedxml.ElementTree as ET

            root = ET.fromstring(r.content)
            for _ in root.iter("position"):
                return True
            return False
        except Exception as e:
            logger.debug("_probe_personio('%s', tld=%s) parse error: %s", slug, tld, e)
            continue
    return False


def _probe_bamboohr(slug: str) -> bool:
    """Return True if slug has at least one active BambooHR posting.

    Probes the public careers widget at /jobs/embed2.php and counts
    ``<li id="bhrPositionID_...">`` items. Tenants without open jobs serve
    a 200 with an empty widget — empty-list pitfall pattern.

    Args:
        slug: BambooHR subdomain (e.g. 'acme' for acme.bamboohr.com).

    Returns:
        True if the slug resolves to a BambooHR tenant with active jobs.
    """
    url = f"https://{slug}.bamboohr.com/jobs/embed2.php"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        # Substring check avoids loading a full HTML parser just for the probe.
        return "bhrPositionID_" in r.text
    except Exception as e:
        logger.debug("_probe_bamboohr('%s') failed: %s", slug, e)
        return False


def _probe_teamtailor(slug: str) -> bool:
    """Return True if slug has at least one active Teamtailor posting.

    Probes the public unkeyed JSON:API at /api/jobs. Tenants without active
    jobs return ``{"data": []}`` — empty-list pitfall, same as others.

    Args:
        slug: Teamtailor subdomain (e.g. 'acme' for acme.teamtailor.com).

    Returns:
        True if the slug resolves to a Teamtailor tenant with active jobs.
    """
    url = f"https://{slug}.teamtailor.com/api/jobs"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        items = data.get("data") or []
        return isinstance(items, list) and len(items) > 0
    except Exception as e:
        logger.debug("_probe_teamtailor('%s') failed: %s", slug, e)
        return False


def _probe_workable(slug: str) -> bool:
    """Return True if slug resolves to a Workable tenant with active jobs.

    Probes the public widget endpoint
    ``https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true``
    which returns ``{"name": ..., "jobs": [...]}``. Empty-jobs path is a
    miss (same pitfall pattern as Lever/Recruitee/etc.).

    Args:
        slug: Workable account slug (first path segment of apply.workable.com URL).
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    try:
        r = requests.get(url, params={"details": "true"}, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        jobs = data.get("jobs") or []
        return isinstance(jobs, list) and len(jobs) > 0
    except Exception as e:
        logger.debug("_probe_workable('%s') failed: %s", slug, e)
        return False


def _probe_jobvite(slug: str) -> bool:
    """Return True if slug resolves to a live Jobvite hosted career page.

    Jobvite has no public unauthenticated JSON API; this probe only
    verifies that ``https://jobs.jobvite.com/{slug}`` resolves to a 200
    page (which it does for any active tenant, including those whose
    careers redirect to a custom domain). A 200 here is necessary but
    not sufficient for a real scanner -- see
    ``_platforms_jobvite.py`` for the stub scanner rationale.

    Args:
        slug: Jobvite tenant slug (first path segment of jobs.jobvite.com URL).
    """
    url = f"https://jobs.jobvite.com/{slug}"
    try:
        # allow_redirects=True so custom-domain tenants (e.g. Victaulic ->
        # careers.victaulic.com) still register as live.
        r = requests.get(url, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_jobvite('%s') failed: %s", slug, e)
        return False


def _probe_paylocity(guid: str) -> bool:
    """Return True if guid resolves to a Paylocity tenant with active jobs.

    Probes the public v2 feed at
    ``https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}``
    which returns ``{"organization": ..., "jobs": [...]}``. Empty-jobs is a miss.

    Args:
        guid: Paylocity tenant GUID (UUID-shaped, extracted from
            ``/recruiting/jobs/All/{guid}`` careers URL).
    """
    url = f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        jobs = data.get("jobs") or []
        return isinstance(jobs, list) and len(jobs) > 0
    except Exception as e:
        logger.debug("_probe_paylocity('%s') failed: %s", guid, e)
        return False


def _probe_rippling(slug: str) -> bool:
    """Return True if slug resolves to a Rippling tenant with active jobs.

    Probes the public v2 board API at
    ``https://ats.rippling.com/api/v2/board/{slug}/jobs`` which returns
    ``{"items": [...], "page": N, ...}``. Empty-items is a miss.

    Args:
        slug: Rippling board slug (first path segment of ats.rippling.com URL).
    """
    url = f"https://ats.rippling.com/api/v2/board/{slug}/jobs"
    try:
        r = requests.get(url, params={"pageSize": 1}, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        items = data.get("items") or []
        return isinstance(items, list) and len(items) > 0
    except Exception as e:
        logger.debug("_probe_rippling('%s') failed: %s", slug, e)
        return False


def _probe_successfactors(slug: str) -> bool:
    """Return True if slug resolves to a live SuccessFactors job board.

    Slug format: ``"{host}|{company_id}"`` (e.g. ``"career2.successfactors.eu|SwissRe"``).
    Fetches the public XML feed at
    ``https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML``
    and returns True only if the body contains ``<Job-Listing>`` AND at least one
    ``<Job>`` (or ``<JobTitle>``). This avoids false positives on plain SEO sitemaps.

    Args:
        slug: SuccessFactors slug in "host|company_id" format.
    """
    try:
        host, company_id = slug.split("|")
    except ValueError:
        logger.debug("_probe_successfactors('%s'): invalid slug format", slug)
        return False

    url = f"https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        # Check for job-bearing content, not just "valid XML"
        content = r.text
        return "<Job-Listing" in content and ("<Job>" in content or "<JobTitle>" in content)
    except Exception as e:
        logger.debug("_probe_successfactors('%s') failed: %s", slug, e)
        return False


def _probe_adp(slug: str) -> bool:
    """Return True if slug resolves to a live ADP Workforce Now job board.

    Slug format: client ID UUID (e.g. ``"a6717ebc-f6a8-4a51-856b-f7ebd573645e"``).
    Fetches the public JSON feed at
    ``https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions``
    with ``cid={slug}`` and returns True only if the response contains at least one
    ``jobRequisitions`` item. This avoids false positives on unrelated endpoints.

    Args:
        slug: ADP client ID UUID.
    """
    url = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    params = {
        "cid": slug,
        "ccId": "19000101_000001",
        "lang": "en_US",
        "locale": "en_US",
    }
    try:
        r = requests.get(url, params=params, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        reqs = data.get("jobRequisitions") or []
        return isinstance(reqs, list) and len(reqs) > 0
    except Exception as e:
        logger.debug("_probe_adp('%s') failed: %s", slug, e)
        return False


def _probe_tesla(slug: str) -> bool:
    """Return True if Tesla careers page is accessible.

    Tesla is single-tenant — slug is ignored (constant "tesla"). The probe
    checks if the careers page returns 200 and contains Tesla-related content.
    Note: Tesla uses anti-bot protection (Akamai/PerimeterX), so this probe
    may return False even when the page is live. The scanner requires an
    authenticated browser session or a live environment that isn't bot-flagged.

    Args:
        slug: Ignored (Tesla is single-tenant).
    """
    url = "https://www.tesla.com/careers"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code == 200 and ("tesla" in r.text.lower() or "careers" in r.text.lower()):
            return True
    except Exception as e:
        logger.debug("_probe_tesla('%s') failed: %s", slug, e)
    return False
