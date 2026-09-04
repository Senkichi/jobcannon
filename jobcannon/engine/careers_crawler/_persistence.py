# PORTED from job_finder/web/careers_crawler/_persistence.py @ 23644885615a78d509d73c8b1c640b7d49b4a089 (private job-cannon). Ledger L-0465.
"""Persistence helpers for the careers crawler.

After a tier produces a list of `dict` jobs for a company, the
orchestrator calls these helpers to:
- Upsert each scraped job (creating a `Job` model object) into the
  `jobs` table.
- Stamp the company's `careers_crawl_last_at`, `last_scanned_at`, and
  `jobs_found_total` columns.
- Append a row to `company_scan_log` for the per-run audit trail via
  `svc.record_scan_outcome`.
- On a per-company exception, only stamp `careers_crawl_last_at` so a
  consistently-failing company doesn't block stalest-first ordering.

# PORT-SEAM: private also stamps a fourth companies column here,
# `careers_crawl_tier`. No jobcannon schema (m0001-m0023 checked) carries
# that column -- it is a pre-existing baseline-port gap unrelated to this
# row's three HOLD columns (L-0274/L-0276/L-0293, landed by this port's
# sibling migration m0023), tracked at
# https://github.com/Senkichi/jobcannon/issues/347. Dropped from the
# companies UPDATE below rather than silently widening m0023's declared
# scope; `tier_used` stays a parameter (unused in the body) so the write
# can be restored with no call-site change once #347 lands.

The `Job` and `ParsedJob` imports are kept lazy inside `_upsert_and_log`,
matching the sibling `ats_scanner/_run.py` / `_run_html.py` ports' own
convention for the same two modules (private's own rationale -- "not
needed when the crawler runs in TESTING mode" -- doesn't apply to the
engine, which has no such mode; the lazy import is kept for parity with
those siblings instead).
"""

from __future__ import annotations

import logging

from jobcannon.engine.careers_crawler._bench_predicate import BENCH_CRAWLER_SOURCE
from jobcannon.engine.identity_evidence import (
    _extract_identity_evidence,
    _identity_evidence_accepts,
    _name_to_slug,
)
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)


def _scraped_job_identity_evidence(scraped_job: dict) -> tuple[set[str], set[str]]:
    """Build anchored/loose identity-evidence slug sets from a scraped job dict.

    Positive evidence can come from:

    * JSON-LD ``hiringOrganization.name`` (anchored exact-brand field)
    * the page ``<title>`` and ``og:site_name`` (parsed from a synthetic HEAD
      snippet when the raw page HTML is not available)

    Absence of any of these fields is fail-open: tiers that don't surface
    page-level identity (sitemap, API fallbacks, etc.) retain the historical
    seed-trust behavior.
    """
    anchored: set[str] = set()
    loose: set[str] = set()

    org = scraped_job.get("hiring_organization")
    if org:
        org_slug = _name_to_slug(org)
        if org_slug:
            anchored.add(org_slug)

    title = scraped_job.get("page_title", "")
    og = scraped_job.get("og_site_name", "")
    if title or og:
        snippet = "<html><head>"
        if title:
            snippet += f"<title>{title}</title>"
        if og:
            snippet += f'<meta property="og:site_name" content="{og}"/>'
        snippet += "</head></html>"
        anch, loose_ev = _extract_identity_evidence(snippet)
        anchored |= anch
        loose |= loose_ev

    return anchored, loose


def _job_identity_conflicts(scraped_job: dict, company_name: str) -> bool:
    """True when the scraped job carries positive employer-identity evidence
    that conflicts with ``company_name``.

    Absence of evidence is *not* treated as a conflict: tiers that don't
    surface page-level identity keep the existing seed-trust behavior.
    """
    anchored, loose = _scraped_job_identity_evidence(scraped_job)
    if not anchored and not loose:
        return False
    return not _identity_evidence_accepts(company_name, anchored, loose)


def _upsert_and_log(
    jobs: list[dict],
    company_id: int,
    company_name: str,
    now: str,
    summary: dict,
    all_new_job_keys: list[str],
    tier_used: str,  # PORT-SEAM: unused pending #347 (see module docstring)
    failure_reason: str | None = None,
) -> None:
    """Upsert discovered jobs and update company timestamps.

    ``failure_reason`` is persisted on the ``company_scan_log`` row so the
    5-strike penalty-box predicate can key strikes on broken attempts only
    (#1725, W4). The caller (``crawl_careers_batch``) passes ``no_title_match``
    (clean: live board, no matching title — not a strike) or a broken reason
    (``zero_jobs`` etc. — a strike) when the ai_nav tier attributed the zero,
    ``BENCH_UNATTRIBUTED_ZERO_HIT_REASON`` for any other crawler-origin
    zero-hit (T2.3, D9 — auditable, still a strike), and ``None`` (the
    default) only on a success (``jobs`` non-empty). This function itself
    stays a thin pass-through — ``None`` still writes NULL for any caller that
    does not attribute its zero-hit (e.g. pre-T2.3 legacy behaviour); the
    predicate treats NULL as a strike, preserving pre-W4 behaviour for rows
    the crawler cannot attribute.
    """
    # PORT-SEAM: db_path param dropped -- svc.connection_factory() is a
    # zero-positional-arg contract (services.py), unlike private's
    # standalone_connection(db_path). No caller ports this same PR
    # (crawl_careers_batch is a later unit), so there is no call site whose
    # signature this needs to preserve.
    svc = get_services()

    from jobcannon.engine.models import Job
    from jobcannon.engine.parsed_job import DenylistedCompanyError, ListingTileError, ParsedJob

    company_jobs_found = len(jobs)
    company_jobs_new = 0
    summary["jobs_found"] += company_jobs_found

    with svc.connection_factory() as upsert_conn:  # PORT-SEAM: seam (L-0465)
        for scraped_job in jobs:
            try:
                # Identity gate: if the scraped job carries positive
                # employer-identity evidence (JSON-LD hiringOrganization,
                # og:site_name, or page-title edge segments) that conflicts
                # with the seed company name, drop it silently rather than
                # attribute an acquirer's jobs to a stale acquired-company
                # identity (#1333).
                if _job_identity_conflicts(scraped_job, company_name):
                    logger.debug(
                        "careers_crawler: dropping '%s' for %s — identity evidence conflicts",
                        scraped_job.get("title", "<no title>"),
                        company_name,
                    )
                    continue

                # P2.2 (D-1, D-5): set Job.location from the scraped dict.
                # JSON-LD jobLocation and gazetteer-validated URL-slug hints
                # are carried here; upsert_job routes them through
                # parse_locations → locations_structured (D-5 funnel).
                # Do NOT pass locations_raw via source_meta: a non-empty
                # locations_raw with empty locations_structured triggers I-07
                # (LocationShapeError in ParsedJob.from_job) and drops the job
                # entirely. Only Job.location is set; upsert_job derives
                # locations_raw via split_multi_locations and locations_structured
                # via its parse_locations fallback (_jobs.py:316–342).
                job = Job(
                    title=scraped_job["title"],
                    company=company_name,
                    location=scraped_job.get("location") or "",
                    source="careers_crawl",
                    source_url=scraped_job.get("url") or "",
                    salary_min=None,
                    salary_max=None,
                    description=scraped_job.get("description", ""),
                )
                # Phase 48.07: build ParsedJob explicitly; the upsert_job
                # Job-shim is gone.
                try:
                    parsed = ParsedJob.from_job(job)
                except (DenylistedCompanyError, ListingTileError):
                    # Denylisted company (I-10) or result-count tile (I-14,
                    # #211): both are hard drops — skip silently.
                    continue
                result = svc.upsert_job(
                    upsert_conn, parsed, company_id=company_id
                )  # PORT-SEAM: seam call (L-0465)
                if result.kind == "inserted":
                    summary["jobs_new"] += 1
                    company_jobs_new += 1
                    # #223: enqueue the PERSISTED key (clean_title-normalized).
                    all_new_job_keys.append(result.dedup_key)
            except Exception as job_err:
                error_msg = f"{company_name} job error: {job_err}"
                summary["errors"].append(error_msg)
                logger.warning("careers_crawler job error: %s", error_msg)

    with svc.connection_factory() as ts_conn:  # PORT-SEAM: seam (L-0465)
        ts_conn.execute(
            """UPDATE companies
               SET careers_crawl_last_at = ?,
                   last_scanned_at = ?,
                   jobs_found_total = (
                       SELECT COUNT(*) FROM jobs WHERE company_id = ?
                   )
               WHERE id = ?""",  # PORT-SEAM: careers_crawl_tier write dropped (#347, see module docstring)
            (now, now, company_id, company_id),
        )
        # WI-07 (D10): jobs_found = scraped/found count, jobs_matched = same
        # (the crawler has no post-scrape title filter, so found == matched),
        # jobs_new = rows actually inserted. This corrects the historical swap
        # (jobs_found <- new, jobs_matched <- found); the m209595428 migration
        # back-fills rows written before this fix.
        if svc.record_scan_outcome is not None:  # PORT-SEAM: optional seam (L-0465)
            svc.record_scan_outcome(
                ts_conn,
                company_id=company_id,
                source=BENCH_CRAWLER_SOURCE,  # PORT-SEAM: shared constant, not a re-literal (L-0463)
                jobs_found=company_jobs_found,
                jobs_matched=company_jobs_found,
                jobs_new=company_jobs_new,
                failure_reason=failure_reason,
                scanned_at=now,
            )

        ts_conn.commit()

    summary["companies_crawled"] += 1

    if company_jobs_found:
        logger.info(
            "careers_crawler: %s — %d jobs found (%d new) [%s]",
            company_name,
            company_jobs_found,
            company_jobs_new,
            tier_used,
        )


def _update_timestamp_on_error(
    company_id: int,
    now: str,
) -> None:
    """Update crawl timestamp on error so company doesn't block the queue."""
    # PORT-SEAM: db_path param dropped -- see _upsert_and_log's PORT-SEAM note.
    svc = get_services()
    try:
        with svc.connection_factory() as err_conn:  # PORT-SEAM: seam (L-0465)
            err_conn.execute(
                "UPDATE companies SET careers_crawl_last_at = ? WHERE id = ?",
                (now, company_id),
            )
            err_conn.commit()
    except Exception:
        pass
