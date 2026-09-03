# PORTED from job_finder/web/ats_scanner/_run_playwright.py @ 4c65f020a404de6504a21593875685db99d3cd65 (private job-cannon). Ledger L-0020.
"""Playwright-class ATS scan path — JS-rendered, no-public-API boards (iCIMS).

Parallel architecture to the requests-based ``PlatformScanner`` registry
path in ``_run.py``. iCIMS (and future JS-rendered, no-API platforms) cannot
ride the ``slug -> list[dict]`` registry contract because they need a live
browser to render the board. This module owns that lifecycle:

1. ``_run_playwright_scan`` is the phase — it queries all Playwright-class
   companies, opens **one** ``sync_playwright()`` block + ``chromium`` browser
   for the whole batch (never one browser per company), and drives each
   company through the scanner.
2. ``run_playwright_platform_scan`` is the per-company driver — the analog of
   ``_registry.run_platform_scan``: fetch → title gate → normalize → log.
3. Upserts route through ``_run._upsert_one_ats_api_job`` (shared with the
   requests path) so iCIMS jobs land in ``jobs`` identically and get picked
   up by the shared Phase D scoring loop.

The ``_run <-> _run_playwright`` import cycle is broken the same way
``_run_html`` breaks it: the shared ``_run`` helpers are imported
function-locally, so importing this module at ``_run`` load time does not
re-enter ``_run``.

Extracted as a discrete submodule (mirroring ``_run_html.py``) to keep each
ats_scanner file under the house line cap.
"""

from __future__ import annotations

import logging
import sqlite3
import time

# PORT-SEAM: bump_empty_scan_counter/record_scan_outcome (WI-06/WI-13) are
# private-only job_finder.db helpers with no ScanServices equivalent yet;
# deferred pending a ScanServices addition (out of this row's scope, needs
# coordinated services.py + _run.py work) -- see the deferred-call comments
# below (L-0020, same carve-out as the L-0019 sibling).
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from jobcannon.engine.ats_prober import _handle_scan_error, _is_transient_error
from jobcannon.engine.ats_registry import PLAYWRIGHT_PLATFORMS
from jobcannon.engine.ats_registry import PLAYWRIGHT_SCANNERS as _PLAYWRIGHT_SCANNERS
from jobcannon.engine.services import (
    get_services,
)  # PORT-SEAM: seam kept; private drops it for a direct careers_crawler/db_helpers import (L-0020)

logger = logging.getLogger(__name__)

# The Playwright-class fetch registry (``_PLAYWRIGHT_SCANNERS``) and its platform
# set (``PLAYWRIGHT_PLATFORMS``) are now derived in ``jobcannon.engine.ats_registry``
# — the single source of truth — and imported above. Phase A excludes
# ``PLAYWRIGHT_PLATFORMS`` so ``_scan_one_company_via_ats_api`` never sees these
# platforms and logs "Unknown ATS platform"; this phase owns them instead.

# Default load-more click budget when ``config.ats.icims_max_load_more_clicks``
# is unset. Matches the scanner module's own default.
_DEFAULT_MAX_LOAD_MORE = 5


def playwright_platform_exclusion_clause() -> str:
    """SQL fragment excluding Playwright-class platforms from the requests path.

    Returns a ``(ats_platform IS NULL OR ats_platform NOT IN (...))`` clause
    built from the internal ``PLAYWRIGHT_PLATFORMS`` constant. The values are
    a hardcoded lowercase code constant (never user input), so inlining them
    as quoted literals is injection-safe — there are no bind parameters to
    thread through the existing f-string-composed Phase A queries.
    """
    quoted = ",".join(f"'{p}'" for p in sorted(PLAYWRIGHT_PLATFORMS))
    return f"(ats_platform IS NULL OR ats_platform NOT IN ({quoted}))"


def run_playwright_platform_scan(
    scanner: PlaywrightPlatformScanner,
    browser,
    slug: str,
    target_titles: list,
    exclusions: list,
    *,
    max_load_more: int = _DEFAULT_MAX_LOAD_MORE,
) -> tuple[list[dict], int]:
    """Run one Playwright-class scan: render → title gate → normalize → log.

    The browser-owning analog of ``_registry.run_platform_scan``: every raw
    posting that ``_title_matches`` accepts is normalized via
    ``scanner.posting_to_job`` and appended. The debug count log mirrors the
    requests-path shape.

    Args:
        scanner: The platform's ``PlaywrightPlatformScanner`` value object.
        browser: Playwright ``Browser`` owned by the caller's lifecycle.
        slug: Per-company platform identifier (iCIMS tenant subdomain).
        target_titles: Title-match keywords for inclusion.
        exclusions: Title-match keywords for exclusion (AND-NOT).
        max_load_more: Per-board "load more" click budget.

    Returns:
        Tuple of (matched_job_dicts, skipped_count) where skipped_count is
        the number of postings filtered by title exclusions. Empty on render
        error or no matches.
    """
    from jobcannon.engine.ats_platforms import _title_matches

    postings = list(scanner.fetch_postings(browser, slug, max_load_more=max_load_more))

    results: list[dict] = []
    skipped_count = 0
    for posting in postings:
        title = scanner.title_of(posting)
        if not _title_matches(title, target_titles, exclusions):
            skipped_count += 1
            continue
        job_dict = scanner.posting_to_job(posting, slug)
        if job_dict is not None:
            results.append(job_dict)

    logger.debug(
        "scan_%s('%s'): %d postings fetched, %d matched, %d skipped by title filter",
        scanner.name,
        slug,
        len(postings),
        len(results),
        skipped_count,
    )
    return results, skipped_count


def _playwright_phase_query(company_names: list[str] | None = None) -> str:
    """SQL for the Playwright phase cohort (mirrors Phase A's status gate).

    # PORT-SEAM: private WI-03 (#1828) removes the high-score-history gate
    # outright. The public `_run.py` caller (out of this row's scope) still
    # passes `high_score_threshold` positionally into every Phase A2 call, so
    # dropping the parameter here would be a call-site TypeError. Keep the
    # neutralized-to-TRUE `_high_score_history_clause` call (see its
    # docstring in _run.py) until `_run.py`'s own WI-03 row lands (L-0020).
    """
    from jobcannon.engine.ats_scanner._run import _high_score_history_clause

    quoted = ",".join(f"'{p}'" for p in sorted(PLAYWRIGHT_PLATFORMS))
    company_filter = ""
    if company_names:
        placeholders = ",".join("?" * len(company_names))
        company_filter = f"AND name_raw IN ({placeholders})"

    return f"""SELECT id, name_raw, ats_platform, ats_slug
           FROM companies
           WHERE ats_platform IN ({quoted})
             AND (
                 (ats_probe_status = 'hit' AND scan_enabled = TRUE) -- # PORT-SEAM: ats_scan_enabled rename deferred (L-0021 sibling; not yet landed)
                 OR
                 (ats_probe_status = 'error' AND scan_enabled = TRUE  -- # PORT-SEAM: ats_scan_enabled rename deferred (L-0020)
                  AND (retry_after IS NULL OR retry_after < datetime('now')))
             )
             AND {_high_score_history_clause("last_scanned_at")} -- # PORT-SEAM: merged_into_id omitted (L-0020 carve-out; column absent from public schema)
             {company_filter}"""


def count_playwright_eligible(
    conn: sqlite3.Connection,
    threshold: int,  # PORT-SEAM: L-0020, see docstring
    company_names: list[str] | None = None,
) -> int:
    """Count Playwright-phase companies subject to the high-score gate.

    `threshold` is accepted for call-site parity but no longer bound:
    _high_score_history_clause is neutralized to TRUE (zero params) in this
    hosted port — see its docstring in _run.py. # PORT-SEAM: L-0020
    """
    params = list(company_names) if company_names else []

    row = conn.execute(
        _playwright_phase_query(company_names).replace(
            "SELECT id, name_raw, ats_platform, ats_slug", "SELECT COUNT(*)", 1
        ),
        tuple(params),
    ).fetchone()
    return int(row[0]) if row else 0


def _run_playwright_scan(
    conn: sqlite3.Connection,
    db_path: str,
    config: dict,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    high_score_threshold: int,  # PORT-SEAM: kept for call-site parity (L-0020; see _playwright_phase_query)
    tracker=None,
    company_names: list[str] | None = None,
    deadline_monotonic: float | None = None,
    run_id: str | None = None,
) -> None:
    """Phase A2: scan Playwright-class companies (iCIMS) under one browser.

    Batches every eligible Playwright-platform company under a single
    ``sync_playwright()`` + ``chromium.launch()`` block. A no-op when no such
    companies exist or when Playwright is not installed (optional heavy dep).

    `high_score_threshold` is accepted for call-site parity but no longer
    bound: _high_score_history_clause is neutralized to TRUE (zero params)
    in this hosted port — see its docstring in _run.py. # PORT-SEAM: L-0020

    ``deadline_monotonic`` is the scan-wide soft deadline shared
    with Phases A and C; when set, the phase stops starting new companies once
    it passes. The already-open browser is still closed in ``finally``.
    """
    params = list(company_names) if company_names else []

    companies = conn.execute(_playwright_phase_query(company_names), tuple(params)).fetchall()
    if not companies:
        return

    max_load_more = int(
        config.get("ats", {}).get("icims_max_load_more_clicks", _DEFAULT_MAX_LOAD_MORE)
    )

    # PORT-SEAM: private resolves this via careers_crawler's PEP-562
    # __getattr__ hook; the public careers_crawler package ports only
    # _title_contract/_title_filters and has no such hook, so importing
    # playwright directly here (matching ats_prober.py's static_fallthrough
    # tier4 pattern) is the correct public form (L-0020).
    try:
        from playwright.sync_api import sync_playwright  # PORT-SEAM: L-0020, see comment above
    except ImportError as exc:
        logger.warning("Playwright not installed — skipping iCIMS scan phase: %s", exc)
        return

    scanned = 0
    truncated = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for company in companies:
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    truncated = True
                    break
                _scan_one_company_via_playwright(
                    conn,
                    db_path,
                    company,
                    browser,
                    target_titles,
                    title_exclusions,
                    summary,
                    all_new_job_keys,
                    max_load_more,
                    run_id=run_id,
                )
                scanned += 1
                if tracker is not None:
                    tracker.tick()
                # Polite delay between companies (rendering is heavier than API).
                time.sleep(0.5)
        finally:
            try:
                browser.close()
            except Exception:
                logger.debug("iCIMS scan: browser.close() failed", exc_info=True)

    if truncated:
        summary["truncated"] = True
        logger.info(
            "ATS Phase A2 (Playwright) truncated after %d/%d companies (scan deadline reached)",
            scanned,
            len(companies),
        )


def _scan_one_company_via_playwright(
    conn: sqlite3.Connection,
    db_path: str,
    company,  # sqlite3.Row
    browser,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    max_load_more: int,
    run_id: str | None = None,
) -> None:
    """Render + scan a single Playwright-class company; upsert + log + retry-track.

    Models ``_run._scan_one_company_via_ats_api`` but dispatches through the
    ``_PLAYWRIGHT_SCANNERS`` map with the shared browser. Upserts reuse the
    requests-path helper so jobs land identically and feed Phase D scoring.
    """
    from jobcannon.engine.ats_scanner._run import _upsert_one_ats_api_job

    # PORT-SEAM: svc resolved per-call via get_services() (L-0020).
    svc = get_services()
    company_id = company["id"]
    company_name = company["name_raw"]
    platform = company["ats_platform"]
    slug = company["ats_slug"]
    now = utc_now_iso()

    logger.info("ATS scan (playwright): scanning %s (%s/%s)", company_name, platform, slug)

    scanner = _PLAYWRIGHT_SCANNERS.get(platform)
    if scanner is None:
        # Defensive: the phase query only selects PLAYWRIGHT_PLATFORMS, so this
        # is unreachable unless the map and the constant drift apart.
        logger.warning("No Playwright scanner for platform '%s' (%s)", platform, company_name)
        return

    try:
        job_dicts, skipped_title_filter = run_playwright_platform_scan(
            scanner,
            browser,
            slug,
            target_titles,
            title_exclusions,
            max_load_more=max_load_more,
        )

        company_jobs_found = len(job_dicts)
        summary["jobs_discovered"] += company_jobs_found

        # PORT-SEAM: company_jobs_new / record_scan_outcome (WI-07/WI-13,
        # L-0020) tracking deferred -- keeps the prior company_scan_log
        # INSERT below.
        with svc.connection_factory() as scan_conn:  # PORT-SEAM: seam (L-0020)
            for job_dict in job_dicts:
                _upsert_one_ats_api_job(
                    conn,
                    scan_conn,
                    company_name,
                    job_dict,
                    summary,
                    all_new_job_keys,
                    company_id=company_id,
                    ats_platform=platform,
                )

        # PORT-SEAM: L-0020 (record_scan_outcome deferred, see above)
        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, skipped_title_filter)
               VALUES (?, ?, ?, ?)""",
            (company_id, now, company_jobs_found, skipped_title_filter),
        )
        conn.execute(
            """UPDATE companies
               SET last_scanned_at = ?,
                   jobs_found_total = jobs_found_total + ?
               WHERE id = ?""",
            (now, company_jobs_found, company_id),
        )
        # PORT-SEAM: bump_empty_scan_counter (#1823, WI-06) deferred with
        # record_scan_outcome above -- consecutive_empty_scans stays
        # unmaintained on this path until that hook lands (L-0020).
        conn.commit()
        summary["companies_scanned"] += 1

    except Exception as company_err:
        error_msg = f"{company_name}: {company_err}"
        summary["errors"].append(
            error_msg
        )  # PORT-SEAM: _record_scan_error deferred (L-0020; needs _run.py's WI-05 row)
        logger.error("ATS scan (playwright) error for '%s': %s", company_name, company_err)

        if _is_transient_error(company_err):
            try:
                _handle_scan_error(conn, company_id, company_name, str(company_err), now)
            except Exception as retry_err:
                # PORT-SEAM: wrap preserved byte-exact for fidelity diff (L-0020)
                # fmt: off
                logger.warning(
                    "Failed to update retry state for '%s': %s", company_name, retry_err
                )
                # fmt: on
                # PORT-SEAM: end fmt guard (L-0020)

        try:
            # PORT-SEAM: record_scan_outcome(error=..., failure_reason=...)
            # deferred (see module import note) -- keeps the prior
            # company_scan_log error-row INSERT (L-0020).
            conn.execute(
                """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                   VALUES (?, ?, 0, ?)""",
                (company_id, now, str(company_err)),
            )
            conn.commit()
        except Exception:
            logger.debug("failed to insert error scan log for %s", company_name, exc_info=True)
