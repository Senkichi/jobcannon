# PORTED from job_finder/web/careers_crawler/__init__.py @ 46d5a2a1f27179d075efc5572efeded3ba2a0266 (private job-cannon). Ledger L-0461.
"""Tier-escalation orchestration for the careers crawler.

Split out of ``__init__.py`` (design note PR-4): ``_crawl_companies`` (the
per-worker, per-company escalation chain that walks tier cache -> api-cache
-> sitemap -> static -> url-param -> embedded-json -> Playwright) plus the
opportunistic ATS-link-discovery helpers it calls (``_try_ats_link_promotion``,
``_ats_link_discovery_due``, ``_stamp_ats_link_discovery``,
``_fetch_careers_landing_html``, ``_try_opportunistic_ats_link_promotion``).

# PORT-SEAM: every ``db_path: str`` parameter this module's private source
# threaded through the tier calls is DROPPED. Every callee below
# (``_try_cached_tier``, ``_try_static_extract``, ``_try_playwright_active``/
# ``_try_playwright_extract``, ``_cache_api_endpoint``/``_clear_api_cache``,
# ``record_legitimacy_flag``, ``_upsert_and_log``/``_update_timestamp_on_error``)
# already dropped it in favor of ``svc.connection_factory()`` (zero-arg) when
# each was ported/landed (L-0443, L-0464, L-0465, L-0469). This module's own
# new ATS-link-discovery helpers follow the same package-wide convention for
# consistency -- every other function in this package already made this
# trade, so keeping ``db_path`` on only this one function would be the odd
# one out, not a fidelity gain.

# PORT-SEAM: the ai-nav tier-4 escalation block (``_try_ai_navigation`` call,
# guarded by ``ai_nav_enabled``) is DELETED, along with the ``ai_nav_reason``
# sink variable and its ``failure_reason`` branch. ``ai_career_navigator``
# DIES (ledger L-0133) -- see also ``_summary.py``'s PORT-SEAM note on the
# matching ``ai_nav_*`` summary-key deletions.

# PORT-SEAM: ``_try_ats_link_promotion`` accesses ``promote_from_careers_link``
# via ``get_services().prober_extensions`` directly (matching the sibling
# ``_autoheal_seam.py``'s direct-seam-access convention within this same
# package), NOT via ``jobcannon.engine.ats_prober``'s module-global
# ``set_prober_extensions``/``_prober_extensions`` indirection -- that
# indirection exists only because ``ats_prober.py`` itself cannot see
# ScanServices directly (see that module's own docstring); this module has
# no such constraint. The call is ported faithfully to the private
# signature -- ``promote_from_careers_link(conn, company_id, platform, slug,
# page_url=page_url, config=config)`` -- WITHOUT the extra
# ``reenable_scan=`` kwarg the sibling ``ats_prober.py`` call sites pass
# (`jobcannon/engine/ats_prober.py`'s "Fix 3" cohort-based re-enable logic);
# that kwarg has no analog in the private ``careers_crawler`` source this
# row carries, so inventing it here would not be a port.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any

import requests

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.careers_crawler._api_cache import (
    _cache_api_endpoint,
    _clear_api_cache,
    _try_cached_api,
)
from jobcannon.engine.careers_crawler._bench_predicate import BENCH_UNATTRIBUTED_ZERO_HIT_REASON
from jobcannon.engine.careers_crawler._cohort_legitimacy import (
    evaluate_cohort_legitimacy,
    record_legitimacy_flag,
)
from jobcannon.engine.careers_crawler._embedded_json_tier import _try_embedded_json_extract
from jobcannon.engine.careers_crawler._persistence import (
    _update_timestamp_on_error,
    _upsert_and_log,
)
from jobcannon.engine.careers_crawler._playwright_tier import (
    _try_playwright_active,
    _try_playwright_extract,
)
from jobcannon.engine.careers_crawler._sitemap_tier import _try_sitemap_extract
from jobcannon.engine.careers_crawler._static_tier import _try_static_extract
from jobcannon.engine.careers_crawler._summary import _SUMMARY_KEYS, _new_summary
from jobcannon.engine.careers_crawler._tier_cache import _try_cached_tier
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)

_POLITE_DELAY = 1.0  # Seconds between companies

# Opportunistic ATS-link discovery cooldown (#1931): when the careers landing
# page has to be fetched specially for ATS-link discovery (no Playwright-rendered
# DOM available -- e.g. a cheap tier already found jobs), re-fetching it every
# crawl for a company with no supported-platform link underneath is wasted
# work. The crawler stamps ``companies.ats_link_discovery_last_at`` on each
# attempt and skips the opportunistic fetch when the last attempt is within
# this many days. Overridable via ``careers_crawl.ats_link_discovery_cooldown_days``.
_ATS_LINK_DISCOVERY_COOLDOWN_DAYS = 7


def _try_ats_link_promotion(
    html: str,
    page_url: str,
    company_id: int,
    company_name: str,
    config: dict,
    local_summary: dict,
) -> bool:
    """Discover an outbound ATS link in a rendered custom page and promote (#453).

    Classifies the highest-specificity Greenhouse/Lever/Ashby/Workday/
    SmartRecruiters link in *html* via ``best_ats_candidate`` and, on a clean
    (non-conflicting) hit, promotes the company to that existing scanner
    through the host-injected ``promote_from_careers_link`` writer. Increments
    ``local_summary['ats_link_promoted']`` and returns ``True`` only when the
    company was actually flipped to ``hit`` (caller then skips remaining
    tiers). Fail-open: any error, or no prober-extensions bundle registered,
    is swallowed so discovery never breaks the crawl.
    """
    from jobcannon.engine.careers_crawler._ats_link_discovery import best_ats_candidate

    svc = get_services()
    ext = svc.prober_extensions  # PORT-SEAM: host-injectable bundle (L-0461)
    if ext is None or getattr(ext, "promote_from_careers_link", None) is None:
        return False

    try:
        candidate = best_ats_candidate(html, page_url)
        if candidate is None:
            return False
        platform, slug = candidate
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0461)
            res = ext.promote_from_careers_link(
                conn,
                company_id,
                platform,
                slug,
                page_url=page_url,
                config=config,
            )
        if res.get("outcome") == "promoted":
            local_summary["ats_link_promoted"] += 1
            logger.info(
                "careers_crawler: ats_link_promoted %s -> %s/%s via %s",
                company_name,
                platform,
                slug[:48],
                page_url,
            )
            return True
        logger.debug(
            "careers_crawler: ats_link discovery for %s outcome=%s",
            company_name,
            res.get("outcome"),
        )
        return False
    except Exception as exc:
        logger.debug(
            "careers_crawler: ats_link discovery failed for %s: %s",
            company_name,
            exc,
        )
        return False


def _ats_link_discovery_due(company_id: int, cooldown_days: float) -> bool:
    """True when the opportunistic ATS-link fetch cooldown has elapsed (#1931).

    Reads ``companies.ats_link_discovery_last_at``; a NULL stamp (never
    attempted) is always due. A stamp within ``cooldown_days`` of now is not
    due. Best-effort: any DB error is swallowed and treated as due (fail-open
    so a transient DB hiccup never silently disables discovery).
    """
    svc = get_services()
    try:
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0461)
            row = conn.execute(
                "SELECT ats_link_discovery_last_at FROM companies WHERE id = ?",
                (company_id,),
            ).fetchone()
            if row is None:
                return True
            last_at = row[0]
            if not last_at:
                return True
            # Compare against SQLite's ``datetime('now')`` so the stamp and the
            # gate share the same clock (UTC, second-resolution), matching how
            # ``careers_crawl_last_at`` is gated in ``crawl_careers_batch``.
            due = conn.execute(
                "SELECT ? < datetime('now', ? || ' days')",
                (last_at, f"-{cooldown_days}"),
            ).fetchone()[0]
            return bool(due)
    except Exception as exc:
        logger.debug(
            "careers_crawler: ats_link_discovery cooldown check failed for %d: %s",
            company_id,
            exc,
        )
        return True


def _stamp_ats_link_discovery(company_id: int) -> None:
    """Record that an opportunistic ATS-link discovery attempt ran (#1931).

    Stamps ``companies.ats_link_discovery_last_at = datetime('now')`` so the
    cooldown gate suppresses a redundant re-fetch on the next crawl. Best-effort:
    a stamp failure is logged at debug and swallowed (it only means the next
    crawl re-attempts -- a bounded, harmless extra fetch).
    """
    svc = get_services()
    try:
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0461)
            conn.execute(
                "UPDATE companies SET ats_link_discovery_last_at = datetime('now') WHERE id = ?",
                (company_id,),
            )
            conn.commit()
    except Exception as exc:
        logger.debug(
            "careers_crawler: failed to stamp ats_link_discovery_last_at for %d: %s",
            company_id,
            exc,
        )


def _fetch_careers_landing_html(careers_url: str) -> str | None:
    """Cheap static GET of the careers landing page for ATS-link discovery (#1931).

    A single bounded ``fetch_with_deadline`` GET (same helper the static tier
    uses) returning the response body, or ``None`` on any failure. This is the
    opportunistic HTML source when no Playwright-rendered DOM is available
    (e.g. a cheap tier already found jobs, so Playwright never ran).
    """
    try:
        resp = fetch_with_deadline(
            careers_url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.debug(
            "careers_crawler: opportunistic ATS-link landing fetch failed for %s: %s",
            careers_url,
            exc,
        )
        return None


def _try_opportunistic_ats_link_promotion(
    *,
    careers_url: str,
    company_id: int,
    company_name: str,
    config: dict,
    local_summary: dict,
    rendered_html: list[str] | None,
) -> bool:
    """Opportunistic ATS-link discovery, decoupled from the zero-jobs gate (#1931).

    Sources the careers landing-page HTML and runs ``_try_ats_link_promotion``
    regardless of how many jobs the generic tiers found.

    HTML sourcing:
      - When ``rendered_html`` is non-empty (Playwright already rendered the
        page), reuse it at zero marginal fetch cost -- no cooldown gate.
      - Otherwise, fetch the landing page via a cheap static GET, gated by the
        per-company cooldown.

    ``ats_link_discovery_last_at`` is stamped on every attempt (rendered or
    fetched) so the cooldown gates future cheap-GET re-attempts. Returns True
    only when the company was actually promoted to ``hit``.
    """
    crawl_cfg = config.get("careers_crawl", {})
    cooldown_days = crawl_cfg.get(
        "ats_link_discovery_cooldown_days", _ATS_LINK_DISCOVERY_COOLDOWN_DAYS
    )

    if rendered_html:
        html = rendered_html[-1]
    else:
        if not _ats_link_discovery_due(company_id, cooldown_days):
            return False
        html = _fetch_careers_landing_html(careers_url)
        if not html:
            # Record the attempt so the cooldown suppresses an immediate
            # re-fetch next crawl of a persistently-unfetchable landing page.
            _stamp_ats_link_discovery(company_id)
            return False

    promoted = _try_ats_link_promotion(
        html,
        careers_url,
        company_id,
        company_name,
        config,
        local_summary,
    )
    _stamp_ats_link_discovery(company_id)
    return promoted


def _crawl_companies(
    companies: list,
    config: dict,
    target_titles: list[str],
    title_exclusions: list[str],
) -> tuple[dict, list[str]]:
    """Crawl companies in parallel with per-worker Playwright browsers.

    Each worker gets its own Playwright context + browser instance (sync API
    is not thread-safe). Companies are distributed round-robin so stalest-first
    ordering is preserved within each batch.

    Returns:
        (merged_summary, all_new_keys) -- summary counters and list of new job dedup_keys.
    """
    from jobcannon.engine.careers_page_interactions import (
        deduplicate_keywords,
        probe_url_params,
    )

    crawl_cfg = config.get("careers_crawl", {})
    max_workers = crawl_cfg.get("max_workers", 4)
    interactive_enabled = crawl_cfg.get("interactive_enabled", True)
    search_keywords = deduplicate_keywords(target_titles)

    # --- Per-worker function (own browser + DB connection) ---
    def _crawl_worker(company_batch: list) -> tuple[dict, list[str]]:
        local_summary: dict[str, Any] = _new_summary()
        local_new_keys: list[str] = []

        # Lazy import: playwright is optional (not in base dependencies).
        # Accessing the package attribute triggers __getattr__ which imports
        # playwright and stores it in module globals for subsequent lookups.
        import jobcannon.engine.careers_crawler as _cc

        _sp = _cc.sync_playwright

        # Lazy import of the scrape-host blocklist predicate (careers_scraper).
        # Kept lazy to avoid a top-level careers_scraper <-> careers_crawler
        # import cycle (careers_scraper imports careers_crawler._title_filters
        # at module scope).
        from jobcannon.engine.careers_scraper import _is_blocklisted_scrape_host

        with _sp() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for company in company_batch:
                    company_id = company["id"]
                    company_name = company["name_raw"]
                    careers_url = company["careers_url"]
                    api_endpoint = company["careers_api_endpoint"]
                    cached_tier = company["careers_crawl_tier"]
                    now = utc_now_iso()
                    tier_used = "static"
                    # Holds the Playwright-rendered DOM so the #453 ATS-link
                    # discovery pass can reuse it without a second navigation.
                    rendered_html: list[str] = []
                    promoted_via_ats_link = False
                    # Whether ATS-link discovery already ran for this company
                    # this crawl (#1931). Prevents the opportunistic
                    # post-chain pass from re-running when the in-chain pass
                    # already attempted it.
                    ats_link_attempted = False
                    # Set when the sitemap tier's cohort-legitimacy gate
                    # withholds a cohort as aggregator-suspected -- short-
                    # circuits the rest of the escalation chain for this
                    # company this run.
                    legitimacy_flagged = False

                    # Aggregator/blog-repost host blocklist (#1622): enforced
                    # at the per-company ingestion boundary, BEFORE any tier
                    # runs, since this chain fetches careers_url directly and
                    # never routes through careers_scraper.scrape_careers_page.
                    if _is_blocklisted_scrape_host(careers_url):
                        logger.info(
                            "careers_crawler: skipping blocklisted aggregator host %s (%s)",
                            company_name,
                            careers_url,
                        )
                        continue

                    logger.info(
                        "careers_crawler: crawling %s via %s",
                        company_name,
                        careers_url,
                    )

                    try:
                        jobs: list[dict] = []

                        # ATS-link discovery is gated on this flag at two
                        # points (#1931): inside the zero-jobs chain (on the
                        # free Playwright-rendered DOM) and after the chain
                        # (opportunistic, decoupled from job yield). Defined
                        # here, before the chain, so the post-chain block can
                        # read it even when a cheap tier found jobs and the
                        # chain body was skipped.
                        ats_link_enabled = config.get("careers_crawl", {}).get(
                            "ats_link_discovery_enabled",
                            True,
                        )

                        # === Tier cache: try last-successful tier first ===
                        # Skip cache replay for `static` and `sitemap` -- both
                        # are cheap pre-static tiers and always run at the top
                        # of the full chain, so cache replay would be redundant.
                        if cached_tier and cached_tier not in ("static", "sitemap"):
                            jobs = _try_cached_tier(
                                cached_tier,
                                browser,
                                company,
                                careers_url,
                                api_endpoint,
                                target_titles,
                                title_exclusions,
                                search_keywords,
                                config,
                                company_id,
                                local_summary,
                            )
                            if jobs:
                                tier_used = cached_tier

                        # === Full escalation chain (if cache miss) ===
                        if not jobs:
                            # Fast path: cached API endpoint
                            if api_endpoint:
                                api_jobs = _try_cached_api(
                                    api_endpoint,
                                    target_titles,
                                    title_exclusions,
                                )
                                if api_jobs is not None:
                                    jobs = api_jobs
                                    tier_used = "api_cached"
                                    local_summary["api_cached"] += 1
                                else:
                                    _clear_api_cache(company_id)

                            # Tier 0.5: Sitemap / RSS -- pre-static cheap
                            # probe. Also runs the cohort-legitimacy gate: a
                            # large cohort whose sampled postings show
                            # positive evidence of belonging to multiple
                            # employers is withheld rather than imported, and
                            # `sitemap_flag` short-circuits the rest of this
                            # company's escalation chain.
                            if not jobs and tier_used != "api_cached":
                                sitemap_flag: list[str] = []
                                sitemap_jobs = _try_sitemap_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                    company_name=company_name,
                                    config=config,
                                    flag_sink=sitemap_flag,
                                )
                                if sitemap_jobs:
                                    jobs = sitemap_jobs
                                    tier_used = "sitemap"
                                    local_summary["sitemap_hits"] += 1
                                elif sitemap_flag:
                                    legitimacy_flagged = True
                                    record_legitimacy_flag(company_id, sitemap_flag[0])
                                    local_summary["legitimacy_flagged"] += 1
                                    logger.warning(
                                        "careers_crawler: %s flagged for cohort-legitimacy "
                                        "review (%s) -- skipping remaining tiers this run",
                                        company_name,
                                        sitemap_flag[0],
                                    )

                            # Tier 1: Static HTML
                            if not jobs and tier_used != "api_cached" and not legitimacy_flagged:
                                static_result = _try_static_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                )
                                if static_result:
                                    jobs = static_result
                                    tier_used = "static"

                            # Tier 2: URL param search
                            if not jobs and tier_used != "api_cached" and not legitimacy_flagged:
                                if search_keywords:
                                    param_jobs = probe_url_params(
                                        careers_url,
                                        search_keywords,
                                        target_titles,
                                        title_exclusions,
                                    )
                                    if param_jobs:
                                        jobs = param_jobs
                                        tier_used = "url_param"
                                        local_summary["url_param_hits"] += 1

                            # Tier 2.5: Embedded JSON extraction
                            if not jobs and tier_used != "api_cached" and not legitimacy_flagged:
                                embedded_jobs = _try_embedded_json_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                )
                                if embedded_jobs:
                                    jobs = embedded_jobs
                                    tier_used = "embedded_json"
                                    local_summary["embedded_json_hits"] += 1

                            # Tier 3: Playwright active
                            if not jobs and tier_used != "api_cached" and not legitimacy_flagged:
                                if interactive_enabled:
                                    pw_jobs, discovered_api = _try_playwright_active(
                                        browser,
                                        careers_url,
                                        target_titles,
                                        title_exclusions,
                                        search_keywords,
                                        config,
                                        html_sink=rendered_html,
                                    )
                                    jobs = pw_jobs
                                    tier_used = "playwright"
                                    local_summary["playwright_rendered"] += 1

                                    if discovered_api:
                                        _cache_api_endpoint(
                                            company_id,
                                            discovered_api,
                                        )
                                else:
                                    jobs = _try_playwright_extract(
                                        browser,
                                        careers_url,
                                        target_titles,
                                        title_exclusions,
                                        html_sink=rendered_html,
                                    )
                                    tier_used = "playwright"
                                    local_summary["playwright_rendered"] += 1

                            # === ATS-link discovery (#453/#1931): custom-site dead end ===
                            # Playwright rendered the page but title-filtered
                            # extraction found 0 jobs. Harvest an outbound
                            # ATS-board link from the already-rendered DOM and
                            # promote the company to the matching existing
                            # scanner -- no second navigation, no new
                            # extractor. On a hit, skip the remaining tiers.
                            if (
                                not jobs
                                and ats_link_enabled
                                and not legitimacy_flagged
                                and rendered_html
                                and not ats_link_attempted
                            ):
                                promoted_via_ats_link = _try_opportunistic_ats_link_promotion(
                                    careers_url=careers_url,
                                    company_id=company_id,
                                    company_name=company_name,
                                    config=config,
                                    local_summary=local_summary,
                                    rendered_html=rendered_html,
                                )
                                ats_link_attempted = True

                            # PORT-SEAM: private Tier 4 (AI-navigated, replay
                            # cached recipe or discover new via
                            # ``_try_ai_navigation``) is DELETED here.
                            # ``ai_career_navigator`` DIES (ledger L-0133) --
                            # see module docstring above.

                        # === Orchestrator-level cohort-legitimacy gate (#1921) ===
                        # The sitemap tier runs the gate internally (the
                        # `sitemap_flag` / `legitimacy_flagged` handoff above)
                        # and short-circuits the remaining escalation chain.
                        # Every OTHER tier that can import a cohort (api_cached,
                        # static, url_param, embedded_json, playwright) is
                        # gated HERE, on the assembled per-company cohort,
                        # AFTER the tier returns postings and BEFORE import.
                        #
                        # Must run BEFORE the opportunistic ATS-link promotion
                        # block below: promotion's identity check derives
                        # evidence_host from this company's OWN crawled
                        # careers page, so it self-satisfies regardless of
                        # whether the harvested jobs actually belong to this
                        # company. Only this content-based verdict can catch
                        # that case, so the promotion block must read `jobs`
                        # only after this gate has had a chance to empty it.
                        if jobs and not legitimacy_flagged and tier_used != "sitemap":
                            verdict = evaluate_cohort_legitimacy(company_name, jobs, config)
                            if verdict.flagged:
                                legitimacy_flagged = True
                                record_legitimacy_flag(company_id, verdict.reason)
                                local_summary["legitimacy_flagged"] += 1
                                logger.warning(
                                    "careers_crawler: %s flagged for "
                                    "cohort-legitimacy review (%s, tier=%s) "
                                    "-- withholding %d posting(s) from import",
                                    company_name,
                                    verdict.reason,
                                    tier_used,
                                    len(jobs),
                                )
                                jobs = []

                        # === Opportunistic ATS-link discovery (#1931) ===
                        # Decoupled from the zero-jobs/Playwright-only gate.
                        # Runs AFTER the chain when a cheap tier found jobs
                        # but no Playwright DOM is in hand: fetches the
                        # landing page via a cheap static GET (cooldown-gated)
                        # and promotes on a clean supported-platform hit. A
                        # promotion here does NOT discard the current crawl's
                        # jobs -- they are still imported below.
                        #
                        # Runs strictly AFTER the #1921 gate above: reading
                        # `jobs` post-gate means a flagged cohort has already
                        # been emptied to `[]` here, so the `jobs` condition
                        # below naturally withholds promotion for aggregators.
                        if (
                            jobs
                            and ats_link_enabled
                            and not promoted_via_ats_link
                            and not legitimacy_flagged
                            and not rendered_html
                            and not ats_link_attempted
                        ):
                            promoted_via_ats_link = _try_opportunistic_ats_link_promotion(
                                careers_url=careers_url,
                                company_id=company_id,
                                company_name=company_name,
                                config=config,
                                local_summary=local_summary,
                                rendered_html=rendered_html,
                            )
                            ats_link_attempted = True

                        # The per-crawl failure_reason for the company_scan_log
                        # row. Only meaningful on a zero-hit (jobs empty).
                        # PORT-SEAM: private also branched on an
                        # ``ai_nav_reason`` sink populated by the deleted
                        # ai_nav tier (``no_title_match`` = clean, not a
                        # strike; a broken reason = strike). With that tier
                        # gone, every zero-hit here writes the
                        # BENCH_UNATTRIBUTED_ZERO_HIT_REASON sentinel, exactly
                        # as the private code already did for every non-ai_nav
                        # zero-hit (T2.3, D9) -- no behavior change for the
                        # tiers this port carries.
                        if not jobs:
                            failure_reason = BENCH_UNATTRIBUTED_ZERO_HIT_REASON
                        else:
                            failure_reason = None
                        _upsert_and_log(
                            jobs,
                            company_id,
                            company_name,
                            now,
                            local_summary,
                            local_new_keys,
                            tier_used,
                            failure_reason=failure_reason,
                        )

                    except Exception as company_err:
                        error_msg = f"{company_name}: {company_err}"
                        local_summary["errors"].append(error_msg)
                        logger.error(
                            "careers_crawler error for '%s': %s",
                            company_name,
                            company_err,
                        )
                        _update_timestamp_on_error(company_id, now)

                    time.sleep(_POLITE_DELAY)
            finally:
                browser.close()

        return local_summary, local_new_keys

    # --- Distribute companies round-robin across workers ---
    batches = [companies[i::max_workers] for i in range(max_workers)]

    merged_summary: dict[str, Any] = dict.fromkeys(_SUMMARY_KEYS, 0)
    merged_summary["errors"] = []
    all_new_keys: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_crawl_worker, batch) for batch in batches if batch]
        for future in concurrent.futures.as_completed(futures):
            try:
                worker_summary, worker_keys = future.result()
                for key in _SUMMARY_KEYS:
                    merged_summary[key] += worker_summary.get(key, 0)
                merged_summary["errors"].extend(worker_summary.get("errors", []))
                all_new_keys.extend(worker_keys)
            except Exception as worker_err:
                merged_summary["errors"].append(f"Worker error: {worker_err}")
                logger.error("careers_crawler worker failed: %s", worker_err)

    return merged_summary, all_new_keys
