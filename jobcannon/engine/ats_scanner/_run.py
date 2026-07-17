"""ATS scan orchestrator + Phase A/B/D/E helpers.

run_ats_scan is the public entry; the underscore-prefixed helpers below
implement four of the five scan phases (A: ATS API, B: Homepage discovery,
D: Scoring, E: Activity-feed log). Phase C (HTML fallback) lives in
_run_html.py because its careers_scraper import-graph is independent of
the ATS-API path.

Each phase helper mutates `summary` (and where relevant, `all_new_job_keys`)
in place to match the original inline-loop semantics. Refactoring to a
return-value protocol is deferred to S8.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime

from jobcannon.engine import ats_prober
from jobcannon.engine.classification import derive_classification
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_platforms import SCANNERS_BY_NAME
from jobcannon.engine.ats_platforms._concurrency import get_scan_concurrency
from jobcannon.engine.ats_platforms._registry import (
    BoardGoneError,
    run_platform_scan,
)
from jobcannon.engine.ats_prober import _handle_scan_error, _is_transient_error
from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS
from jobcannon.engine.ats_scanner._run_html import _run_html_fallback_scan
from jobcannon.engine.ats_scanner._run_playwright import (
    _run_playwright_scan,
    count_playwright_eligible,
    playwright_platform_exclusion_clause,
)
from jobcannon.engine.description_formatter import strip_html_to_text
from jobcannon.engine.services import get_services

# Platform key -> PlatformScanner. The scan dispatch consumes the SINGLE central
# registry (jobcannon.engine.ats_platforms.SCANNERS_BY_NAME) directly — registering
# a scanner there is the only step to make a platform scannable; there is no
# second list to keep in sync. (A parallel hardcoded dict here silently dropped
# the Amazon/Microsoft/Eightfold adapters from the live scan — see #529 fallout.)
# NON_SCANNABLE platforms (jobvite/google) are caught before dispatch, so their
# presence in the registry is harmless.
_PLATFORM_SCANNERS = SCANNERS_BY_NAME


def _json_default_for_cache(obj: object) -> object:
    """Serialize dataclass and datetime/date values in the scan cache.

    Mirrors the db/_jobs.py pattern of calling ``asdict()`` on frozen
    dataclass instances and ``.isoformat()`` on datetime/date instances
    before JSON persistence.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _cache_scan_result(
    conn: sqlite3.Connection,
    company_id: int,
    job_dicts: list[dict],
    now: str,
) -> None:
    """Persist the full board job-dict list for enrichment's DB-backed cache.

    Writes into companies.last_scan_postings_json + last_scan_cached_at.
    Best-effort: a serialization or DB error is logged and swallowed so the
    scan result is still processed normally.
    """
    try:
        postings_json = json.dumps(job_dicts, default=_json_default_for_cache)
    except (TypeError, ValueError) as e:
        logger.warning("Failed to serialize scan cache for company %s: %s", company_id, e)
        return

    try:
        conn.execute(
            "UPDATE companies SET last_scan_postings_json = ?, last_scan_cached_at = ? WHERE id = ?",
            (postings_json, now, company_id),
        )
    except Exception as e:
        logger.warning("Failed to write scan cache for company %s: %s", company_id, e)


@dataclass(frozen=True, slots=True)
class _CompanyScanResult:
    """Delta result from scanning one company in a worker thread.

    Workers return this instead of mutating shared state; the main thread
    merges all deltas via as_completed.
    """

    company_name: str
    jobs_discovered: int
    jobs_new: list[str]
    skipped_title_filter: int
    error: str | None = None
    board_demoted: bool = False
    # Per-job upsert errors (e.g. a single posting failing Job/upsert_job
    # validation) — distinct from `error`, which is a company-level failure
    # (fetch_postings raising). The serial path's _upsert_one_ats_api_job
    # appends these directly into the shared summary["errors"] list; the
    # worker path calls it per-job against a throwaway local_summary dict
    # (it cannot safely share the orchestrator's summary across threads), so
    # those per-job errors must be threaded back through this field and
    # merged into summary["errors"] by the caller — otherwise they are
    # silently dropped, diverging from serial-path behavior.
    job_errors: list[str] = field(default_factory=list)


# scoring_orchestrator.score_and_persist_job and homepage_discoverer's
# run_homepage_discovery don't port (host/Flask-coupled) — both are optional
# ScanServices hooks (svc.score_and_persist_job / svc.run_homepage_discovery),
# resolved per-call via get_services() at each call site below instead of a
# module-level ImportError-guarded import.

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


class _ProgressTracker:
    """Shared counter for Phase A + Phase C company-level progress.

    Both phases iterate companies; tick() bumps the count and forwards
    (scanned, total) to the caller-supplied callback. A failing callback
    must never abort a scan, so the call is wrapped in a broad try.
    """

    __slots__ = ("_callback", "_scanned", "_total")

    def __init__(self, callback: ProgressCallback | None, total: int) -> None:
        self._callback = callback
        self._scanned = 0
        self._total = total

    def tick(self) -> None:
        self._scanned += 1
        if self._callback is None:
            return
        try:
            self._callback(self._scanned, self._total)
        except Exception:
            logger.debug("progress callback raised — continuing scan", exc_info=True)


# Default v3 sub_score sum cutoff for the high-score-history gate (Phase A + C).
# v3 sub_scores are 6 axes x 1-5 each (sum range 6-30). The empirical break
# point for "company has produced relevant work for this profile" is ~20:
# at >=20 the cohort is dominated by apply+consider, below 20 it's dominated
# by reject. Override per-deployment via config.ats.high_score_history_threshold.
_DEFAULT_HIGH_SCORE_THRESHOLD = 20


_ALLOWED_RECENCY_COLUMNS = {"last_scanned_at", "careers_crawl_last_at"}


def _high_score_history_clause(recency_column: str) -> str:
    """SQL fragment for the ats_scan high-score-history gate.

    Companies pass IF either (a) this phase has never run for them
    (`recency_column` IS NULL), (b) they have no scored jobs yet (bootstrap
    pass — new companies need a first scan to build history), OR (c) at
    least one prior job has a v3 sub_score sum >= ?. Use with one bind
    parameter: the threshold integer (typically 20).

    `recency_column` is the phase-specific "last run" timestamp
    (e.g. ``last_scanned_at`` for Phase A/Playwright,
    ``careers_crawl_last_at`` for Phase C). It is interpolated as a SQL
    identifier; callers must pass a hardcoded column name.

    Score-based, not classification-based — the classifier has had
    reliability issues in the past; sub_scores are the underlying signal.
    """
    if recency_column not in _ALLOWED_RECENCY_COLUMNS:
        raise ValueError(f"Invalid recency_column for high-score-history gate: {recency_column}")
    return f"""(
        {recency_column} IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE (j.company = companies.name OR j.company = companies.name_raw)
              AND j.sub_scores_json IS NOT NULL
        )
        OR EXISTS (
            SELECT 1 FROM jobs j
            WHERE (j.company = companies.name OR j.company = companies.name_raw)
              AND j.sub_scores_json IS NOT NULL
              AND (COALESCE(json_extract(j.sub_scores_json, '$.title_fit'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.location_fit'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.comp_fit'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.domain_match'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.seniority_match'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.skills_match'), 0)) >= ?
        )
    )"""


def _ats_identity_not_null_clause() -> str:
    """SQL fragment for the ats_scan NULL-identity guard.

    Companies must have both ats_platform and ats_slug set to be eligible
    for ATS API scanning. This enforces the invariant that ats_probe_status='hit'
    should never coexist with NULL identity columns.

    Use without bind parameters.
    """
    return "(ats_platform IS NOT NULL AND ats_slug IS NOT NULL)"


def _dormancy_gate_clause() -> str:
    """SQL fragment for the ats_scan yield-tiered dormancy cadence gate.

    Companies are skipped when they have exceeded the consecutive empty scan
    threshold AND were scanned recently (within the dormancy interval). This
    implements yield-tiered cadence: dormant boards are scanned less frequently
    but never permanently disabled. A non-zero yield resets the counter
    (instant promotion back to every-scan cadence).

    Use with two bind parameters: threshold (int) and interval_days (int).
    """
    return """(
        consecutive_empty_scans <= ?
        OR last_scanned_at IS NULL
        OR last_scanned_at < datetime('now', '-' || ? || ' days')
    )"""


def _count_phase_a_eligible(
    conn: sqlite3.Connection,
    threshold: int,
    dormancy_threshold: int,
    dormancy_interval_days: int,
    company_names: list[str] | None = None,
) -> int:
    """Count Phase A companies (hit OR retry-eligible error) subject to the gate."""
    company_filter = ""
    params = [threshold, dormancy_threshold, dormancy_interval_days]
    if company_names:
        placeholders = ",".join("?" * len(company_names))
        company_filter = f"AND name_raw IN ({placeholders})"
        params.extend(company_names)

    row = conn.execute(
        f"""SELECT COUNT(*) FROM companies
           WHERE (
               (ats_probe_status = 'hit' AND scan_enabled = 1)
               OR
               (ats_probe_status = 'error' AND scan_enabled = 1
                AND (retry_after IS NULL OR retry_after < datetime('now')))
           )
           AND {playwright_platform_exclusion_clause()}
           AND {_high_score_history_clause("last_scanned_at")}
           AND {_ats_identity_not_null_clause()}
           AND {_dormancy_gate_clause()}
           {company_filter}""",
        tuple(params),
    ).fetchone()
    return int(row[0]) if row else 0


def _count_phase_c_eligible(
    conn: sqlite3.Connection, threshold: int, company_names: list[str] | None = None
) -> int:
    """Count Phase C companies (miss/error with homepage, plus non-scannable hits)."""
    non_scannable = sorted(NON_SCANNABLE_PLATFORMS)
    placeholders = ",".join("?" * len(non_scannable))
    # Non-scannable platforms (e.g. jobvite) have a registered scanner that
    # is intentionally a stub. Their 'hit' companies have a real careers
    # page but no public API, so the HTML fallback is the only productive
    # path — include them here so progress totals are accurate.
    non_scannable_clause = (
        f"OR (ats_probe_status = 'hit' AND ats_platform IN ({placeholders}))"
        if non_scannable
        else ""
    )

    company_filter = ""
    params = [*non_scannable, threshold]
    if company_names:
        company_placeholders = ",".join("?" * len(company_names))
        company_filter = f"AND name_raw IN ({company_placeholders})"
        params.extend(company_names)

    row = conn.execute(
        f"""SELECT COUNT(*) FROM companies
           WHERE (
               ats_probe_status IN ('miss', 'error')
               {non_scannable_clause}
           )
             AND homepage_url IS NOT NULL
             AND scan_enabled = 1
             AND {_high_score_history_clause("careers_crawl_last_at")}
             {company_filter}""",
        tuple(params),
    ).fetchone()
    return int(row[0]) if row else 0


def run_ats_scan(
    db_path: str,
    config: dict,
    progress_callback: ProgressCallback | None = None,
    company_names: list[str] | None = None,
) -> dict:
    """Scan all enabled companies' ATS platforms for keyword-matched job postings.

    Thread-safe: creates its own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    Flow:
    1. Query companies WHERE ats_probe_status='hit' AND scan_enabled=1
    2. For each company, call scan_lever/scan_greenhouse/scan_ashby
    3. Apply keyword filter using config profile.target_titles and exclusions
    4. For each matched job, create Job object and call upsert_job
    5. Collect dedup_keys of newly-discovered jobs
    6. Score new jobs via scoring_orchestrator (v3.0 unified `run_scoring`)
    7. Insert company_scan_log row and update company.last_scanned_at
    8. Insert activity feed entry into runs table
    9. Return summary dict
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag, profile section.
        company_names: Optional list of company names to scan. If None, scans all enabled companies.

    Returns:
        Dict with keys: companies_scanned, jobs_discovered, jobs_new,
        scored, classified_apply, classified_consider, classified_skip,
        classified_reject, errors, degraded_sources.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("run_ats_scan: TESTING mode — skipping API calls")
        return {
            "companies_scanned": 0,
            "jobs_discovered": 0,
            "jobs_new": 0,
            "scored": 0,
            "classified_apply": 0,
            "classified_consider": 0,
            "classified_skip": 0,
            "classified_reject": 0,
            "html_scraped": 0,
            "homepages_discovered": 0,
            "errors": [],
            "degraded_sources": [],
        }

    svc = get_services()
    # Task 3 amendment (single source of truth for the identity trio): this
    # is the SINGLE wiring site that propagates the host's prober-extensions
    # bundle into jobcannon.engine.ats_prober's module-global seam for the
    # duration of the scan, restoring whatever was previously registered
    # afterward. A host that leaves ScanServices.prober_extensions unset
    # simply gets the fail-closed prober defaults (Task 2 amendment, Step 7e).
    prior_prober_extensions = ats_prober._prober_extensions
    ats_prober.set_prober_extensions(svc.prober_extensions)
    try:
        return _run_ats_scan_body(db_path, config, progress_callback, company_names, svc)
    finally:
        ats_prober.set_prober_extensions(prior_prober_extensions)


def _run_ats_scan_body(
    db_path: str,
    config: dict,
    progress_callback: ProgressCallback | None,
    company_names: list[str] | None,
    svc,
) -> dict:
    """Body of ``run_ats_scan``, split out so the public entry point above can
    wrap it in the prober_extensions setup/restore required by the Task 3
    amendment without reindenting the whole function. No logic changes from
    the private source beyond the ScanServices seam rewires below."""
    # Extract keyword filter settings from config
    profile = config.get("profile", {})
    target_titles = profile.get("target_titles", [])
    exclusions_cfg = profile.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    # High-score-history gate: skip companies whose past scored jobs are all
    # below the cutoff. See _high_score_history_clause for semantics.
    high_score_threshold = int(
        config.get("ats", {}).get("high_score_history_threshold", _DEFAULT_HIGH_SCORE_THRESHOLD)
    )

    # Dormancy gate: skip companies with consecutive empty scans above threshold
    # within the dormancy interval. See _dormancy_gate_clause for semantics.
    dormancy_threshold = int(config.get("ats", {}).get("dormancy_threshold", 10))
    dormancy_interval_days = int(config.get("ats", {}).get("dormancy_interval_days", 3))

    # Workday per-board pagination budget (issue #216): threaded explicitly
    # through run_platform_scan -> fetch_postings rather than via a ContextVar.
    # ContextVars do not propagate into ThreadPoolExecutor worker threads, so
    # under the parallel page-fetch pool (issue #1029) the override would
    # silently fall back to the platform default instead of the configured
    # budget.
    workday_max_pages = config.get("ats", {}).get("workday_max_pages")

    # Optional Phase A runtime cap (issue #1130): absent or 0 = no limit.
    # Normalized once at the boundary rather than re-checked at every call
    # site: a negative value is documented as "no limit" (matches the
    # config.example.yaml ">0" semantics) but was previously truthy, so
    # `if runtime_limit_s and elapsed >= runtime_limit_s` would trip
    # immediately after the first tick — treat <= 0 as disabled here so
    # every downstream comparison stays a plain truthiness check.
    runtime_limit_s = config.get("ats", {}).get("runtime_limit_s")
    if runtime_limit_s is not None and runtime_limit_s <= 0:
        runtime_limit_s = None

    # Scan concurrency for Phase A (issue #1030): default 1 preserves serial
    # behavior byte-for-byte; clamped to [1, 6] — see _concurrency.py.
    scan_concurrency = get_scan_concurrency(config)

    summary: dict = {
        "companies_scanned": 0,
        "jobs_discovered": 0,
        "jobs_new": 0,
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "html_scraped": 0,
        "homepages_discovered": 0,
        "skipped_title_filter": 0,
        "errors": [],
        "degraded_sources": [],
    }
    all_new_job_keys: list[str] = []

    with svc.connection_factory() as conn:
        # Compute total upfront so the progress-callback's (scanned, total)
        # pair is stable for the full scan (Phase A + Phase C). Phase B and
        # Phase D aren't per-company iterations and don't tick the tracker.
        # The route's initial _scannable_count is a Phase-A-only estimate;
        # the first callback invocation corrects total on the session row.
        total_companies = (
            _count_phase_a_eligible(
                conn,
                high_score_threshold,
                dormancy_threshold,
                dormancy_interval_days,
                company_names,
            )
            + count_playwright_eligible(conn, high_score_threshold, company_names)
            + _count_phase_c_eligible(conn, high_score_threshold, company_names)
        )
        tracker = _ProgressTracker(progress_callback, total_companies)

        # Phase A — ATS-API scan for confirmed-hit + retry-eligible-error companies.
        _run_ats_api_scan(
            conn,
            db_path,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
            high_score_threshold,
            dormancy_threshold,
            dormancy_interval_days,
            tracker,
            company_names,
            workday_max_pages,
            scan_concurrency,
            runtime_limit_s=runtime_limit_s,
        )

        # Phase A2 — Playwright-class scan (iCIMS): JS-rendered, no-API boards.
        # Batched under a single sync_playwright() lifecycle. Runs after the
        # requests-API phase and before homepage discovery; new jobs feed the
        # shared Phase D scoring loop via all_new_job_keys.
        _run_playwright_scan(
            conn,
            db_path,
            config,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
            high_score_threshold,
            tracker,
            company_names,
        )

        # Phase B — Homepage discovery for companies missing homepage_url. Runs
        # BEFORE the HTML fallback so newly-discovered homepages are available.
        _run_homepage_discovery_phase(db_path, config, summary, company_names)

        # Phase C — HTML fallback for miss/error companies that DO have a homepage.
        _run_html_fallback_scan(
            conn,
            db_path,
            config,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
            high_score_threshold,
            tracker,
            company_names,
        )

        # Phase D — Auto-scoring for newly discovered jobs across both phases.
        _score_new_ats_jobs(conn, config, all_new_job_keys, summary)

        # Phase E — Activity feed entry so Dashboard Recent Activity shows 'ats_scan'.
        _log_ats_scan_run(conn, summary)

    # --- Post-scan detection pass (mirrors pipeline_runner.py:280-291) ---
    # Runs after the connection_factory block has closed, so run_detection
    # opening its own connection cannot self-lock. Both hooks are optional
    # ScanServices fields (health_monitor / pipeline_runner don't port — see
    # the Task 3 seam mapping table): unset => skip detection (no degraded
    # sources reported) and skip the heal pass respectively.
    if svc.run_detection is not None:
        summary["degraded_sources"] = svc.run_detection(db_path, config)

    # Phase C / C5: attempt auto-heal for newly-degraded sources. Flag-gated
    # (autoheal.heal_enabled, default true since D6) and fully error-isolated —
    # a heal failure must never break the scan. Piggybacks this detection pass;
    # no scheduler job.
    if svc.run_heal_pass is not None:
        svc.run_heal_pass(db_path, config, summary["degraded_sources"])

    logger.info(
        "ATS scan complete: %d companies scanned, %d jobs discovered, %d new, %d scored "
        "(apply=%d, consider=%d, skip=%d, reject=%d)",
        summary["companies_scanned"],
        summary["jobs_discovered"],
        summary["jobs_new"],
        summary["scored"],
        summary.get("classified_apply", 0),
        summary.get("classified_consider", 0),
        summary.get("classified_skip", 0),
        summary.get("classified_reject", 0),
    )
    return summary


# ---------------------------------------------------------------------------
# Phase helpers for run_ats_scan
# ---------------------------------------------------------------------------


def _scan_one_company_worker(
    company: sqlite3.Row,
    db_path: str,
    target_titles: list,
    title_exclusions: list,
    workday_max_pages: int | None,
) -> _CompanyScanResult:
    """Worker task: scan one company with its own DB connection and return delta result.

    Per-host request pacing (issue #1030) is enforced once, centrally, in
    ``ats_platforms._http_session.get_session()`` — every platform's HTTP call
    already routes through that shared Session, so there is nothing for this
    worker to gate itself; it just runs today's per-company scan body against
    its own connection.

    Args:
        company: The company row (id, name_raw, ats_platform, ats_slug).
        db_path: Path to the SQLite database.
        target_titles: Title-match keywords for inclusion.
        title_exclusions: Title-match keywords for exclusion.
        workday_max_pages: Optional page budget for paginated platforms.

    Returns:
        A _CompanyScanResult with the delta from this company scan.
    """
    company_id = company["id"]
    company_name = company["name_raw"]
    platform = company["ats_platform"]
    slug = company["ats_slug"]
    now = utc_now_iso()
    svc = get_services()

    # Open our own connection (thread-safe) — connection_factory does not
    # pass check_same_thread=False, so sharing the orchestrator's outer conn
    # across worker threads is forbidden (see class docstring / issue #1030).
    with svc.connection_factory(synchronous="NORMAL") as worker_conn:
        jobs_discovered = 0
        jobs_new: list[str] = []
        skipped_title_filter = 0
        error: str | None = None
        board_demoted = False
        job_errors: list[str] = []

        try:
            if platform in NON_SCANNABLE_PLATFORMS:
                # Registered scanner that intentionally has no public API
                logger.info(
                    "ATS scan: '%s' has no public API for %s; deferring to HTML fallback",
                    platform,
                    company_name,
                )
                job_dicts = []
                raw_job_dicts = []
                skipped_title_filter = 0
            else:
                scanner = _PLATFORM_SCANNERS.get(platform)
                if scanner is None:
                    logger.warning(
                        "Unknown ATS platform '%s' for company '%s'", platform, company_name
                    )
                    job_dicts = []
                    raw_job_dicts = []
                    skipped_title_filter = 0
                else:
                    max_pages_arg = workday_max_pages if platform == "workday" else None
                    job_dicts, skipped_title_filter, raw_job_dicts = run_platform_scan(
                        scanner,
                        slug,
                        target_titles,
                        title_exclusions,
                        max_pages=max_pages_arg,
                        force_fresh=True,
                        conn=worker_conn,
                        return_raw=True,
                    )

            _cache_scan_result(worker_conn, company_id, raw_job_dicts, now)

            company_jobs_found = len(job_dicts)
            jobs_discovered = company_jobs_found

            # Upsert each matched job
            for job_dict in job_dicts:
                # Local summary dict for this worker. Must include "errors" —
                # _upsert_one_ats_api_job appends job-level failures (e.g. a
                # single posting failing DB invariants) into summary["errors"]
                # directly; without the key present that append raises
                # KeyError, which the outer except Exception handler below
                # would misclassify as a company-level failure and abort the
                # rest of this company's jobs. The captured errors are
                # threaded back via job_errors on the returned
                # _CompanyScanResult so the caller can merge them into the
                # real summary["errors"], matching the serial path's
                # behavior.
                local_summary = {
                    "jobs_new": 0,
                    "errors": [],
                }
                local_all_new_job_keys: list[str] = []

                _upsert_one_ats_api_job(
                    worker_conn,  # Outer conn for jd_full promotion
                    worker_conn,  # scan_conn for upsert
                    company_name,
                    job_dict,
                    local_summary,
                    local_all_new_job_keys,
                    company_id=company_id,
                    ats_platform=platform,
                )

                jobs_new.extend(local_all_new_job_keys)
                job_errors.extend(local_summary["errors"])

            # Log company scan
            worker_conn.execute(
                """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, skipped_title_filter)
                   VALUES (?, ?, ?, ?)""",
                (company_id, now, company_jobs_found, skipped_title_filter),
            )

            # Update company
            worker_conn.execute(
                """UPDATE companies
                   SET last_scanned_at = ?,
                       jobs_found_total = jobs_found_total + ?,
                       consecutive_empty_scans = CASE WHEN ? = 0 THEN consecutive_empty_scans + 1 ELSE 0 END
                   WHERE id = ?""",
                (now, company_jobs_found, company_jobs_found, company_id),
            )
            worker_conn.commit()

        except BoardGoneError as gone:
            logger.warning(
                "ATS scan: '%s' board gone (HTTP %d) — demoting %s/%s to miss/platform_slug_gone",
                company_name,
                gone.status,
                platform,
                slug,
            )
            try:
                worker_conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'platform_slug_gone',
                           scan_enabled = 0,
                           last_scanned_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, now, company_id),
                )
                worker_conn.execute(
                    """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                       VALUES (?, ?, 0, ?)""",
                    (company_id, now, f"board gone (HTTP {gone.status})"),
                )
                worker_conn.commit()
            except Exception:
                logger.exception("ATS scan: failed to demote gone board for '%s'", company_name)
            board_demoted = True

        except Exception as company_err:
            error_msg = f"{company_name}: {company_err}"
            error = error_msg
            logger.error("ATS scan error for '%s': %s", company_name, company_err)

            if _is_transient_error(company_err):
                try:
                    _handle_scan_error(
                        worker_conn, company_id, company_name, str(company_err), now
                    )
                except Exception as retry_err:
                    logger.warning(
                        "Failed to update retry state for '%s': %s", company_name, retry_err
                    )

            try:
                worker_conn.execute(
                    """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                       VALUES (?, ?, 0, ?)""",
                    (company_id, now, str(company_err)),
                )
                worker_conn.commit()
            except Exception:
                logger.debug("failed to insert error scan log for %s", company_name, exc_info=True)

    return _CompanyScanResult(
        company_name=company_name,
        jobs_discovered=jobs_discovered,
        jobs_new=jobs_new,
        skipped_title_filter=skipped_title_filter,
        error=error,
        board_demoted=board_demoted,
        job_errors=job_errors,
    )


def _run_ats_api_scan(
    conn: sqlite3.Connection,
    db_path: str,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    high_score_threshold: int,
    dormancy_threshold: int,
    dormancy_interval_days: int,
    tracker: "_ProgressTracker | None" = None,
    company_names: list[str] | None = None,
    workday_max_pages: int | None = None,
    scan_concurrency: int = 1,
    runtime_limit_s: int | None = None,
) -> None:
    """Phase A: scan confirmed-hit companies (and retry-eligible errors) via ATS API."""
    # Query companies with confirmed ATS slug (hit) AND error companies eligible
    # for retry (past their retry_after backoff window). Gated by the
    # high-score-history clause so companies that have only ever produced
    # low-scoring jobs are skipped (bootstrap exception for never-scored).
    # Exclude Playwright-class platforms (iCIMS): they have no requests-only
    # API and are handled by the dedicated Playwright phase. Without this they
    # would fall through to _scan_one_company_via_ats_api's "Unknown ATS
    # platform" warning path.
    # Also gated by the dormancy clause: skip companies with consecutive empty
    # scans above threshold within the dormancy interval.
    company_filter = ""
    params = [high_score_threshold, dormancy_threshold, dormancy_interval_days]
    if company_names:
        placeholders = ",".join("?" * len(company_names))
        company_filter = f"AND name_raw IN ({placeholders})"
        params.extend(company_names)

    companies = conn.execute(
        f"""SELECT id, name_raw, ats_platform, ats_slug
           FROM companies
           WHERE (
               (ats_probe_status = 'hit' AND scan_enabled = 1)
               OR
               (ats_probe_status = 'error' AND scan_enabled = 1
                AND (retry_after IS NULL OR retry_after < datetime('now')))
           )
           AND {playwright_platform_exclusion_clause()}
           AND {_high_score_history_clause("last_scanned_at")}
           AND {_ats_identity_not_null_clause()}
           AND {_dormancy_gate_clause()}
           {company_filter}
           ORDER BY last_scanned_at IS NULL DESC, last_scanned_at ASC""",
        tuple(params),
    ).fetchall()

    total_companies = len(companies)
    start_monotonic = time.monotonic()
    truncated = False

    if scan_concurrency <= 1:
        # Serial path: preserve exact current behavior including 0.5s sleep
        for company in companies:
            _scan_one_company_via_ats_api(
                conn,
                db_path,
                company,
                target_titles,
                title_exclusions,
                summary,
                all_new_job_keys,
                workday_max_pages,
            )
            if tracker is not None:
                tracker.tick()
            # Polite delay between companies (0.5s)
            time.sleep(0.5)
            if runtime_limit_s and time.monotonic() - start_monotonic >= runtime_limit_s:
                truncated = True
                break
    else:
        # Concurrent path: thread pool with per-host pacing. Submit lazily so a
        # runtime limit can stop queuing new companies while in-flight workers
        # finish.
        #
        # Once the cap fires there are two distinct things to do, and only
        # one of them is actually about *submission speed*:
        #   1. Discard whatever is still sitting in the executor's internal
        #      queue (i.e. never got a worker thread) via
        #      executor.shutdown(wait=False, cancel_futures=True), called
        #      the moment truncation is first detected. This is what makes
        #      the cap effective even though submitting every company up
        #      front is essentially instantaneous — cancel_futures discards
        #      not-yet-started work regardless of how much of it was queued.
        #   2. Never abandon a future that has ALREADY started — cancel()
        #      only succeeds on a still-PENDING future, so a RUNNING one
        #      keeps going. The for-loop below therefore never breaks early:
        #      it keeps draining as_completed() to exhaustion so every
        #      already-running worker's result is still merged into
        #      summary/all_new_job_keys before this function returns. No job
        #      a worker discovered can silently skip Phase D scoring, and
        #      Phase A2+ can never start while a worker thread is still
        #      writing (issue #1130 rework — the prior code broke out of
        #      this loop immediately on truncation and called
        #      shutdown(wait=False, cancel_futures=True), which let an
        #      already-running worker's DB write and result race past the
        #      rest of run_ats_scan unmerged).
        executor = ThreadPoolExecutor(max_workers=scan_concurrency)
        future_to_company = {}
        try:
            for company in companies:
                if runtime_limit_s and time.monotonic() - start_monotonic >= runtime_limit_s:
                    truncated = True
                    break
                future = executor.submit(
                    _scan_one_company_worker,
                    company,
                    db_path,
                    target_titles,
                    title_exclusions,
                    workday_max_pages,
                )
                future_to_company[future] = company

            if truncated:
                # Cap already fired while still enqueuing companies: discard
                # whatever hasn't started yet right away instead of waiting
                # for the drain loop below to notice on its first completed
                # future.
                executor.shutdown(wait=False, cancel_futures=True)

            for future in as_completed(future_to_company):
                company = future_to_company[future]
                if future.cancelled():
                    # Discarded by cancel_futures above before it ever got a
                    # worker thread — nothing ran, nothing to merge.
                    continue
                try:
                    result = future.result()

                    # Merge delta into shared state (single-threaded)
                    summary["jobs_discovered"] += result.jobs_discovered
                    summary["jobs_new"] += len(result.jobs_new)
                    all_new_job_keys.extend(result.jobs_new)

                    # Mirror the serial path's semantics: companies_scanned only
                    # counts companies whose scan completed without error or a
                    # board-gone demotion (_scan_one_company_via_ats_api's
                    # `summary["companies_scanned"] += 1` sits on the try
                    # block's success path, unreachable once an exception is
                    # raised). Incrementing unconditionally here would diverge
                    # from serial-path totals whenever a company's scan fails.
                    if result.error is None and not result.board_demoted:
                        summary["companies_scanned"] += 1

                    if result.error:
                        summary["errors"].append(result.error)

                    if result.job_errors:
                        summary["errors"].extend(result.job_errors)

                    if result.board_demoted:
                        summary["boards_demoted"] = summary.get("boards_demoted", 0) + 1

                    if tracker is not None:
                        tracker.tick()

                    # Do NOT break here: keep draining as_completed() so any
                    # future that's already running (and therefore can't be
                    # cancelled) still gets merged below instead of abandoned.
                    if (
                        not truncated
                        and runtime_limit_s
                        and time.monotonic() - start_monotonic >= runtime_limit_s
                    ):
                        truncated = True
                        # Discard whatever's still queued now; anything
                        # already running keeps going and is drained by this
                        # same loop on a later iteration.
                        executor.shutdown(wait=False, cancel_futures=True)

                except Exception as exc:
                    logger.exception(
                        "Worker task failed for company %s: %s", company["name_raw"], exc
                    )
                    summary["errors"].append(f"{company['name_raw']}: {exc}")
                    if tracker is not None:
                        tracker.tick()
                    if (
                        not truncated
                        and runtime_limit_s
                        and time.monotonic() - start_monotonic >= runtime_limit_s
                    ):
                        truncated = True
                        executor.shutdown(wait=False, cancel_futures=True)

        finally:
            # Every future in future_to_company has already been drained by
            # the loop above (cancelled ones are skipped, running ones are
            # waited on via as_completed) — this is a defensive no-op wait
            # in the common case, kept so an exception that escapes the loop
            # before draining finishes still can't return with a worker
            # mid-write.
            executor.shutdown(wait=True, cancel_futures=True)

    if truncated:
        summary["truncated"] = True
        logger.info(
            "ATS Phase A truncated after %d/%d companies (runtime_limit_s=%s)",
            summary["companies_scanned"],
            total_companies,
            runtime_limit_s,
        )


def _scan_one_company_via_ats_api(
    conn: sqlite3.Connection,
    db_path: str,
    company,  # sqlite3.Row
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    workday_max_pages: int | None = None,
) -> None:
    """Scan a single company via its ATS API; upsert + log + retry-track."""
    company_id = company["id"]
    company_name = company["name_raw"]
    platform = company["ats_platform"]
    slug = company["ats_slug"]
    now = utc_now_iso()
    svc = get_services()

    logger.info("ATS scan: scanning %s (%s/%s)", company_name, platform, slug)

    try:
        if platform in NON_SCANNABLE_PLATFORMS:
            # Registered scanner that intentionally has no public API
            # (e.g. jobvite). Skip the no-op run_platform_scan + autoheal
            # break-capture — a steady-state [] from a stub would otherwise
            # look like a "previously-productive platform broke" signal.
            # Phase C (HTML fallback) is the productive path for these and
            # picks up the company in the same scan when homepage_url is set.
            logger.info(
                "ATS scan: '%s' has no public API for %s; deferring to HTML fallback",
                platform,
                company_name,
            )
            job_dicts = []
            raw_job_dicts = []
            skipped_title_filter = 0
        else:
            scanner = _PLATFORM_SCANNERS.get(platform)
            if scanner is None:
                logger.warning(
                    "Unknown ATS platform '%s' for company '%s'", platform, company_name
                )
                job_dicts = []
                raw_job_dicts = []
                skipped_title_filter = 0
            else:
                # Pass conn so run_platform_scan captures the raw pre-filter
                # API response with detect=True (Phase B).  The Phase-A
                # detect=False post-filter hook has been removed; raw capture
                # at the registry chokepoint supersedes it.
                # Only Workday consumes max_pages (issue #1029); other
                # platforms' fetch_postings accept and ignore it.
                # force_fresh=True enforces the discovery-freshness invariant:
                # ats_scan must never serve a cached board, even if a fresh memo
                # entry exists. The fresh result is still written to the memo
                # for read-only consumers (reconciler, resolver, enrichment).
                max_pages_arg = workday_max_pages if platform == "workday" else None
                job_dicts, skipped_title_filter, raw_job_dicts = run_platform_scan(
                    scanner,
                    slug,
                    target_titles,
                    title_exclusions,
                    max_pages=max_pages_arg,
                    force_fresh=True,
                    conn=conn,
                    return_raw=True,
                )

        _cache_scan_result(conn, company_id, raw_job_dicts, now)
        # The cache UPDATE is on the same conn that run_platform_scan used for
        # record_extraction; commit it before opening the second connection
        # used by _upsert_one_ats_api_job to avoid a 30s write-lock deadlock.
        conn.commit()

        company_jobs_found = len(job_dicts)
        summary["jobs_discovered"] += company_jobs_found

        # Upsert each matched job (uses inner connection_factory per Phase A semantics).
        # Opt-in to NORMAL synchronous mode for multi-commit performance win under WAL.
        with svc.connection_factory(synchronous="NORMAL") as scan_conn:
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

        # Log company scan with skipped_title_filter count (issue #849)
        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, skipped_title_filter)
               VALUES (?, ?, ?, ?)""",
            (company_id, now, company_jobs_found, skipped_title_filter),
        )

        # Update company last_scanned_at, jobs_found_total, and consecutive_empty_scans
        # Increment consecutive_empty_scans when company_jobs_found == 0, reset to 0 on any non-zero find
        conn.execute(
            """UPDATE companies
               SET last_scanned_at = ?,
                   jobs_found_total = jobs_found_total + ?,
                   consecutive_empty_scans = CASE WHEN ? = 0 THEN consecutive_empty_scans + 1 ELSE 0 END
               WHERE id = ?""",
            (now, company_jobs_found, company_jobs_found, company_id),
        )
        conn.commit()
        summary["companies_scanned"] += 1

    except BoardGoneError as gone:
        # The board's slug 404/410'd — it no longer resolves. Demote the stale
        # hit (clear scan_enabled + record miss_reason) so we stop firing a dead
        # request at it every scan and the UI reflects reality. Promotion back to
        # hit happens via the loosened /companies/<id>/retry route or a future
        # probe if the slug ever resolves again.
        logger.warning(
            "ATS scan: '%s' board gone (HTTP %d) — demoting %s/%s to miss/platform_slug_gone",
            company_name,
            gone.status,
            platform,
            slug,
        )
        try:
            conn.execute(
                """UPDATE companies
                   SET ats_probe_status = 'miss',
                       miss_reason = 'platform_slug_gone',
                       scan_enabled = 0,
                       last_scanned_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (now, now, company_id),
            )
            conn.execute(
                """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                   VALUES (?, ?, 0, ?)""",
                (company_id, now, f"board gone (HTTP {gone.status})"),
            )
            conn.commit()
        except Exception:
            logger.exception("ATS scan: failed to demote gone board for '%s'", company_name)
        summary["boards_demoted"] = summary.get("boards_demoted", 0) + 1
        return

    except Exception as company_err:
        error_msg = f"{company_name}: {company_err}"
        summary["errors"].append(error_msg)
        logger.error("ATS scan error for '%s': %s", company_name, company_err)

        # Distinguish transient vs permanent failures for retry tracking
        if _is_transient_error(company_err):
            try:
                _handle_scan_error(conn, company_id, company_name, str(company_err), now)
            except Exception as retry_err:
                logger.warning(
                    "Failed to update retry state for '%s': %s", company_name, retry_err
                )

        # Still log the failed scan attempt
        try:
            conn.execute(
                """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                   VALUES (?, ?, 0, ?)""",
                (company_id, now, str(company_err)),
            )
            conn.commit()
        except Exception:
            logger.debug("failed to insert error scan log for %s", company_name, exc_info=True)


def _upsert_one_ats_api_job(
    conn: sqlite3.Connection,
    scan_conn: sqlite3.Connection,
    company_name: str,
    job_dict: dict,
    summary: dict,
    all_new_job_keys: list,
    *,
    company_id: int | None = None,
    ats_platform: str | None = None,
) -> None:
    """Upsert a single ATS-API-discovered job; promote jd_full + comp_data_json on first-seen."""
    svc = get_services()
    try:
        # P1.5 (D-4): the legacy "first-seen salary wins" suppression that NULLed
        # the incoming ATS salary whenever the stored row already had either bound
        # is DELETED. That suppression silently defeated reconciliation — a
        # Greenhouse structured pair could never refresh or correct a stored
        # feed-string guess. Trust-ranked, pair-atomic reconciliation inside
        # upsert_job now decides: an ``ats_structured`` (rank 4) pair overwrites
        # any lower-rank stored pair, and refreshes an equal-rank one.
        from jobcannon.engine.models import Job
        from jobcannon.engine.salary_normalizer import SalaryObservation

        salary_min = job_dict.get("salary_min")
        salary_max = job_dict.get("salary_max")

        job = Job(
            title=job_dict["title"],
            company=company_name,
            location=job_dict.get("location") or "",
            source=job_dict["company_source"],  # 'Lever', 'Greenhouse', 'Ashby'
            source_url=job_dict.get("source_url") or "",
            source_id=job_dict.get("source_id") or "",
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=job_dict.get("salary_currency") or "USD",
            salary_period=job_dict.get("salary_period") or "unknown",
            description=job_dict.get("description") or "",
            posted_date=job_dict.get("posted_date"),
            # ATS platform APIs return audited first-posted timestamps (#363).
            # A platform may downgrade its own marker (Workday relative
            # strings emit 'approximate', #364); default is 'exact'.
            posted_date_precision=(
                job_dict.get("posted_date_precision")
                or ("exact" if job_dict.get("posted_date") else None)
            ),
        )
        from jobcannon.engine.parsed_job import DenylistedCompanyError, ListingTileError, ParsedJob

        # Phase 48.07: construct ParsedJob explicitly. The structured
        # locations from the ATS payload ride in via source_meta — they
        # would otherwise be lost (ParsedJob.from_job carries no Job→
        # locations_structured pathway of its own).
        #
        # P1.5 (D-1/D-4): an ATS structured pay range is the highest-trust
        # salary source (provenance 'ats_structured', rank 4). Tag the writer
        # class and seed the lossless observation log so the reconciler can rank
        # it and the evidence survives even when the canonical pair is later
        # quarantined/overwritten. comp_data_json retains the verbatim API
        # payload for healing.
        _source_meta: dict = {
            "locations_structured": job_dict.get("locations_structured") or [],
        }
        # P1.3 (D-1): a converted capture site builds the lossless observation
        # itself — it carries the RAW per-period values the source asserted (e.g.
        # $64/hour), NOT the annualized canonical pair. Use it verbatim so the
        # append-log records what the source actually said. Scanners not yet
        # converted fall back to synthesizing an observation from the resolved
        # job-dict values below.
        scanner_observation = job_dict.get("salary_observation")
        if scanner_observation is not None:
            _source_meta["salary_provenance"] = (
                job_dict.get("salary_provenance") or "ats_structured"
            )
            _source_meta["salary_observation"] = scanner_observation
        elif salary_min is not None or salary_max is not None:
            from dataclasses import asdict

            _source_meta["salary_provenance"] = "ats_structured"
            # Store the JSON-serializable dict form of the observation (the
            # append-log persists as JSON, not the frozen dataclass).
            _source_meta["salary_observation"] = asdict(
                SalaryObservation(
                    min_value=salary_min,
                    max_value=salary_max,
                    period=job_dict.get("salary_period") or "unknown",
                    currency=job_dict.get("salary_currency") or "USD",
                    provenance="ats_structured",
                    raw_text=job_dict.get("comp_json"),
                )
            )
        try:
            parsed = ParsedJob.from_job(job, source_meta=_source_meta)
        except (DenylistedCompanyError, ListingTileError):
            # Preserve the pre-48.07 shim early-return semantics: a
            # denylisted company (I-10) — or a result-count tile (I-14, #211)
            # — is skipped silently.
            return

        result = svc.upsert_job(
            scan_conn,
            parsed,
            company_id=company_id,
            ats_platform=ats_platform,
        )

        # Promote ATS description to jd_full (DQ-03) — only when jd_full is NULL,
        # preserving any richer description already written by a prior enricher.
        # Routed through set_jd_full() (Phase 46.03) for the content-density gate.
        raw_desc = job_dict.get("description") or ""
        clean_desc = strip_html_to_text(raw_desc) if "<" in raw_desc else raw_desc
        if clean_desc:
            try:
                existing_jd = conn.execute(
                    "SELECT jd_full FROM jobs WHERE dedup_key = ?", (job.dedup_key,)
                ).fetchone()
                if existing_jd is not None and not existing_jd[0]:
                    svc.set_jd_full(
                        conn,
                        job.dedup_key,
                        clean_desc[: svc.jd_storage_max_chars],
                        source="ats_scanner_run",
                    )
            except Exception as e:
                logger.warning(
                    "Failed to promote ATS description to jd_full for %s: %s",
                    job.dedup_key,
                    e,
                )

        if result.kind == "inserted":
            summary["jobs_new"] += 1
            # #223: enqueue the PERSISTED key (clean_title-normalized).
            all_new_job_keys.append(result.dedup_key)

            # Store comp_json for new jobs only (first-seen wins)
            comp_json = job_dict.get("comp_json")
            if comp_json:
                try:
                    conn.execute(
                        "UPDATE jobs SET comp_data_json = ? WHERE dedup_key = ?",
                        (comp_json, job.dedup_key),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to store comp_data_json for %s: %s",
                        job.dedup_key,
                        e,
                    )

            # Store captured structured fields for new jobs only (#451,
            # first-seen wins) — mirrors the comp_data_json post-insert UPDATE
            # rather than threading through the ParsedJob/upsert INSERT contract.
            is_remote = job_dict.get("is_remote")
            employment_type = job_dict.get("employment_type")
            department = job_dict.get("department")
            if is_remote is not None or employment_type is not None or department is not None:
                try:
                    conn.execute(
                        "UPDATE jobs SET is_remote = ?, employment_type = ?, "
                        "department = ? WHERE dedup_key = ?",
                        (is_remote, employment_type, department, job.dedup_key),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to store ATS structured fields for %s: %s",
                        job.dedup_key,
                        e,
                    )

        # Store mutable refresh timestamp on EVERY sighting (not first-seen-wins)
        # so it can diverge from posted_date for repost detection (#575).
        # Uses COALESCE so a later non-NULL value wins and a missing payload value
        # never clobbers a known one.
        refreshed_at = job_dict.get("ats_refreshed_at")
        if refreshed_at is not None:
            try:
                conn.execute(
                    "UPDATE jobs SET ats_refreshed_at = COALESCE(?, ats_refreshed_at) "
                    "WHERE dedup_key = ?",
                    (refreshed_at, result.dedup_key),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to store ats_refreshed_at for %s: %s", result.dedup_key, e)

    except Exception as job_err:
        error_msg = f"{company_name} job error: {job_err}"
        summary["errors"].append(error_msg)
        logger.warning("ATS scan job error: %s", error_msg)


def _run_homepage_discovery_phase(
    db_path: str, config: dict, summary: dict, company_names: list[str] | None = None
) -> None:
    """Phase B: discover homepages for companies missing homepage_url.

    Bounded to free_tier_cap=50 with metered=False so this pre-step stays
    light and never touches Tier 3 (Claude CLI) / Tier 4 (SerpAPI) — the
    dedicated daily 6:30 AM homepage_discovery job is the sole consumer of
    those metered tiers, once per day.

    When company_names is provided, skips homepage discovery since those companies
    should already have their ATS slugs probed (wizard_first_scan use case).
    """
    svc = get_services()
    if svc.run_homepage_discovery is None:
        return
    # Skip homepage discovery when scanning specific companies (wizard_first_scan)
    # since they should already have their ATS slugs probed
    if company_names:
        logger.debug("Homepage discovery skipped for company-filtered scan")
        summary["homepages_discovered"] = 0
        return
    try:
        discovery_result = svc.run_homepage_discovery(
            db_path, config, free_tier_cap=50, metered=False
        )
        logger.info(
            "Homepage discovery: %d checked, %d found",
            discovery_result.get("companies_checked", 0),
            discovery_result.get("homepages_found", 0),
        )
        summary["homepages_discovered"] = discovery_result.get("homepages_found", 0)
    except Exception as disc_err:
        logger.warning("Homepage discovery failed (non-fatal): %s", disc_err)
        summary["homepages_discovered"] = 0


def _score_new_ats_jobs(
    conn: sqlite3.Connection,
    config: dict,
    all_new_job_keys: list,
    summary: dict,
) -> None:
    """Phase D: enrich sparse rows, then score via score_and_persist_job.

    Matches careers_crawl: shell listings (short HTML fallback, thin API text)
    often lack jd_full / salary / location until enrich_job runs.
    """
    # v3.0 (Phase 34 Plan 3 Commit A): uses unified score_and_persist_job;
    # per-classification counters replace haiku_scored / sonnet_evaluated.
    svc = get_services()
    if not all_new_job_keys or svc.score_and_persist_job is None:
        return
    try:
        _enrich_job = svc.enrich_job

        serpapi_key = svc.get_secret("sources.serpapi.api_key", config=config)
        scored_count = 0

        for dedup_key in all_new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue
                job_row = dict(row)

                if _enrich_job is not None and (
                    not job_row.get("jd_full")
                    or job_row.get("salary_min") is None
                    or not job_row.get("location")
                ):
                    try:
                        enriched = _enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            job_row.update(enriched)
                    except Exception as enrich_err:
                        logger.debug(
                            "ATS scan enrichment failed for '%s' (non-fatal): %s",
                            dedup_key,
                            enrich_err,
                        )

                result = svc.score_and_persist_job(
                    job_row,
                    conn,
                    config,
                )
                if result is None:
                    continue
                scored_count += 1
                if getattr(result, "status", None) != "ok" or result.data is None:
                    continue
                cls = derive_classification(
                    result.data.sub_scores,
                    job_row.get("legitimacy_note"),
                    degenerate=getattr(result.data, "degenerate", False),
                )
                key = f"classified_{cls}"
                summary[key] = summary.get(key, 0) + 1
            except Exception as job_err:
                logger.warning(
                    "ATS scoring error for '%s': %s -- continuing",
                    dedup_key,
                    job_err,
                )

        summary["scored"] = scored_count

    except Exception as score_err:
        logger.warning("ATS scan scoring failed (non-fatal): %s", score_err)


def _log_ats_scan_run(conn: sqlite3.Connection, summary: dict) -> None:
    """Phase E: insert one runs-table row so Dashboard Recent Activity shows the scan."""
    try:
        conn.execute(
            "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) VALUES (?, ?, ?, ?, ?)",
            (
                utc_now_iso(),
                "ats_scan",
                summary["jobs_discovered"],
                summary["jobs_new"],
                summary.get("scored", 0),
            ),
        )
        conn.commit()
    except Exception as runs_err:
        logger.warning("Failed to insert ATS scan activity feed entry: %s", runs_err)
