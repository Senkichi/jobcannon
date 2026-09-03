# PORTED from job_finder/web/ats_scanner/_run_html.py @ 4c65f020a404de6504a21593875685db99d3cd65 (private job-cannon). Ledger L-0019.
"""HTML-fallback scan path for ATS-miss / ATS-error companies that have a homepage.

Runs after the ATS-API scan loop in `run_ats_scan`. For each miss/error
company with a homepage_url:
1. Use cached `careers_url` or discover one via `careers_scraper.find_careers_url`.
2. Scrape that page via `careers_scraper.scrape_careers_page`.
3. Upsert each matched job; cache `careers_url` on first discovery.

Extracted from ats_scanner/_run.py during S7c (portfolio cleanup) to keep
each ats_scanner submodule under the 600-line house cap.
"""

import logging
import sqlite3
import time

from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS
from jobcannon.engine.services import get_services  # PORT-SEAM: seam kept; private drops it for a direct careers_scraper import (L-0019)

logger = logging.getLogger(__name__)

# PORT-SEAM: careers_scraper.find_careers_url / scrape_careers_page don't
# port (Flask/db coupled) — both are optional ScanServices hooks
# (svc.find_careers_url / svc.scrape_careers_page); unset means skip the
# HTML-fallback careers path.


def _run_html_fallback_scan(
    conn: sqlite3.Connection,
    db_path: str,
    config: dict,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    high_score_threshold: int,  # PORT-SEAM: kept for call-site parity (L-0019; see below)
    tracker=None,
    company_names: list[str] | None = None,
    deadline_monotonic: float | None = None,
    run_id: str | None = None,
) -> None:
    """Phase C: HTML scrape miss/error companies (+ non-scannable hits) with homepage.

    ``deadline_monotonic`` (issue #1368) is the scan-wide soft deadline shared
    with Phases A and A2; when set, the phase stops starting new companies once
    it passes so the scan exits gracefully with partial results instead of
    being killed mid-company by the scheduler's hard wall-clock timeout.
    # PORT-SEAM: hosted-only addendum (L-0019) -- privately this is also the
    # scheduler's hard wall-clock timeout; hosted it is ScanServices'
    # `scan_deadline_s` bound. `high_score_threshold` stays a parameter (see
    # below) even though private WI-03 dropped the gate it feeds.
    """
    # Eligible cohort:
    #   - ats_probe_status in ('miss', 'error') with a homepage (original gate), OR
    #   - ats_probe_status='hit' for a NON_SCANNABLE_PLATFORMS platform (e.g. jobvite).
    # The second branch is the load-bearing fix for #222: a registered-but-stub
    # scanner used to mark its companies 'hit', which excluded them from this
    # query entirely and silently shadowed the only viable discovery path (the
    # tenant's real careers page, often on a redirected custom domain).
    # PORT-SEAM: private WI-03 (#1828) removes the high-score-history gate
    # outright. The public `_run.py` caller (out of this row's scope) still
    # passes `high_score_threshold` positionally into every Phase C call, so
    # dropping the parameter here would be a call-site TypeError. Keep the
    # parameter and the neutralized-to-TRUE `_high_score_history_clause` call
    # (see its docstring in _run.py) until `_run.py`'s own WI-03 row lands.
    from jobcannon.engine.ats_scanner._run import _high_score_history_clause

    # PORT-SEAM: private drops the ScanServices seam for a module-level,
    # ImportError-guarded direct import of careers_scraper. The public port
    # keeps `find_careers_url`/`scrape_careers_page` as ScanServices hooks
    # (svc.find_careers_url / svc.scrape_careers_page) -- see the module
    # docstring note above and services.py.
    svc = get_services()
    if svc.find_careers_url is None or svc.scrape_careers_page is None:
        return

    non_scannable = sorted(NON_SCANNABLE_PLATFORMS)
    placeholders = ",".join("?" * len(non_scannable))
    non_scannable_clause = (
        f"OR (ats_probe_status = 'hit' AND ats_platform IN ({placeholders}))"
        if non_scannable
        else ""
    )

    company_filter = ""
    # PORT-SEAM: high_score_threshold is accepted for call-site parity but no
    # longer bound: _high_score_history_clause is neutralized to TRUE (zero
    # params) in this hosted port — see its docstring in _run.py.
    params = [*non_scannable]
    if company_names:
        company_placeholders = ",".join("?" * len(company_names))
        company_filter = f"AND name_raw IN ({company_placeholders})"
        params.extend(company_names)

    miss_companies = conn.execute(
        f"""SELECT id, name_raw, homepage_url, careers_url FROM companies
           WHERE (
               ats_probe_status IN ('miss', 'error')
               {non_scannable_clause}
           )
             AND homepage_url IS NOT NULL
             AND scan_enabled = TRUE -- # PORT-SEAM: ats_scan_enabled rename deferred (L-0021 sibling; not yet landed)
             AND careers_crawl_last_at IS NULL
             AND {_high_score_history_clause("careers_crawl_last_at")} -- # PORT-SEAM: merged_into_id omitted (L-0019 carve-out; column absent from public schema)
             {company_filter}
           ORDER BY last_scanned_at IS NULL DESC, last_scanned_at ASC""",
        tuple(params),
    ).fetchall()

    scanned = 0
    truncated = False
    for miss_company in miss_companies:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            truncated = True
            break
        _scan_one_company_via_html(
            conn,
            db_path,
            miss_company,
            config,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
            run_id=run_id,
        )
        scanned += 1
        if tracker is not None:
            tracker.tick()
        # Polite delay — HTML scraping is slower than ATS API calls
        time.sleep(1.0)

    if truncated:
        summary["truncated"] = True
        logger.info(
            "ATS Phase C (HTML fallback) truncated after %d/%d companies (scan deadline reached)",
            scanned,
            len(miss_companies),
        )


def _scan_one_company_via_html(
    conn: sqlite3.Connection,
    db_path: str,
    miss_company,  # sqlite3.Row
    config: dict,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    run_id: str | None = None,
) -> None:
    """Scrape a single miss/error company's careers page; upsert + log + cache careers_url."""
    # PORT-SEAM: svc resolved per-call via get_services() (see module note).
    svc = get_services()
    # Type narrowing: callers gate on these in _run_html_fallback_scan; restate the
    # invariant here so this helper is type-safe in isolation.
    assert svc.find_careers_url is not None and svc.scrape_careers_page is not None
    # PORT-SEAM: private WI-05 routes scanner errors through `_record_scan_error`
    # (a public.engine `_run.py` addition not in this row's scope, L-0019).
    # `run_id` is threaded through for forward-compat but not yet consumed --
    # `record_scan_outcome` (below) is the same deferred dependency.
    miss_company_id = miss_company["id"]
    miss_company_name = miss_company["name_raw"]
    miss_homepage_url = miss_company["homepage_url"]
    now = utc_now_iso()

    logger.info(
        "ATS HTML fallback: scanning %s via homepage %s",
        miss_company_name,
        miss_homepage_url,
    )

    try:
        # Step 1: Use cached careers_url or discover from homepage
        careers_url = miss_company["careers_url"]
        newly_discovered_careers = False
        if not careers_url:
            careers_url = svc.find_careers_url(  # PORT-SEAM: seam call (L-0019)
                miss_homepage_url,
                conn=conn,
                config=config,
            )
            if careers_url:
                newly_discovered_careers = True
        if not careers_url:
            logger.debug(
                "ATS HTML fallback: no careers link found for %s",
                miss_company_name,
            )
            return

        # Step 2: Scrape careers page for keyword-matched jobs.
        # scrape_careers_page enforces _is_blocklisted_scrape_host internally
        # (careers_scraper.py, gate from #1006/#1003) — it returns ([], 0) for
        # a blocklisted aggregator domain (tryapplynow.com, liveblog365.com, …)
        # before any fetch, so no rows are upserted. That gate is the single
        # point of enforcement for the careers_page write path; re-checking the
        # same predicate here on the same careers_url would be a no-op.
        scraped_jobs, skipped_title_filter = svc.scrape_careers_page(  # PORT-SEAM: seam call (L-0019)
            careers_url,
            target_titles,
            title_exclusions,
            conn=conn,
            config=config,
        )

        company_html_found = len(scraped_jobs)
        # PORT-SEAM: company_html_new tracking deferred with record_scan_outcome (see Step 4 note)

        # Step 3: Create Job objects and upsert
        # PORT-SEAM: record_scan_outcome/upsert_job import deferred; upsert_job stays svc.upsert_job below (L-0019)
        from jobcannon.engine.models import Job
        from jobcannon.engine.parsed_job import DenylistedCompanyError, ListingTileError, ParsedJob

        with svc.connection_factory() as html_conn:  # PORT-SEAM: seam (L-0019)
            for scraped_job in scraped_jobs:
                try:
                    job = Job(
                        title=scraped_job["title"],
                        company=miss_company_name,
                        location=scraped_job.get("location", ""),
                        source="careers_page",
                        source_url=scraped_job.get("url") or "",
                        salary_min=None,
                        salary_max=None,
                        description=scraped_job.get("description", ""),
                    )
                    # Phase 48.07: build ParsedJob explicitly; the Job shim
                    # is gone from upsert_job.
                    try:
                        parsed = ParsedJob.from_job(job)
                    except (DenylistedCompanyError, ListingTileError):
                        # Denylist (I-10) or result-count tile (I-14, #211):
                        # both hard-dropped.
                        continue
                    result = svc.upsert_job(html_conn, parsed, company_id=miss_company_id)  # PORT-SEAM: seam call (L-0019)
                    if result.kind == "inserted":
                        summary["jobs_new"] += 1
                        # PORT-SEAM: company_html_new += 1 deferred with record_scan_outcome
                        # #223: enqueue the PERSISTED key (clean_title-normalized).
                        all_new_job_keys.append(result.dedup_key)
                    summary["html_scraped"] += 1
                except Exception as job_err:
                    error_msg = f"{miss_company_name} HTML job error: {job_err}"
                    summary["errors"].append(error_msg)  # PORT-SEAM: _record_scan_error deferred (L-0019; needs _run.py's WI-05 row)
                    logger.warning("ATS HTML fallback job error: %s", error_msg)

        # Step 4: Log company scan
        # PORT-SEAM: record_scan_outcome (run_id / jobs_new tracking, WI-13) is
        # a deferred ScanServices addition (L-0019) -- keeps the prior direct
        # company_scan_log INSERT until that hook lands.
        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, skipped_title_filter)
               VALUES (?, ?, ?, ?)""",
            (miss_company_id, now, company_html_found, skipped_title_filter),
        )

        # Step 5: Update company last_scanned_at, jobs_found_total,
        # and cache newly discovered careers_url for future runs.
        if newly_discovered_careers:
            conn.execute(
                """UPDATE companies
                   SET last_scanned_at = ?,
                       careers_url = ?,
                       jobs_found_total = (
                           SELECT COUNT(*) FROM jobs WHERE company_id = ?
                       )
                   WHERE id = ?""",
                (now, careers_url, miss_company_id, miss_company_id),
            )
        else:
            conn.execute(
                """UPDATE companies
                   SET last_scanned_at = ?,
                       jobs_found_total = (
                           SELECT COUNT(*) FROM jobs WHERE company_id = ?
                       )
                   WHERE id = ?""",
                (now, miss_company_id, miss_company_id),
            )
        conn.commit()

    except Exception as html_err:
        error_msg = f"{miss_company_name} HTML fallback error: {html_err}"
        summary["errors"].append(error_msg)  # PORT-SEAM: _record_scan_error deferred (see above)
        logger.error(
            "ATS HTML fallback error for '%s': %s",
            miss_company_name,
            html_err,
        )
