# PORTED from job_finder/web/careers_crawler/_scoring.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0443 (umbrella -- no ledger row of its own; imported at module scope by careers_crawler/__init__.py, so the package is unlandable without it -- see PR body).
"""Scoring trigger for newly discovered careers-crawl jobs.

After the orchestrator's per-company tiers have produced a list of new
`dedup_key`s, this module enriches each shell row (`jd_full`, salary,
location) and routes it through the unified v3.0 scorer
(`score_and_persist_job`), then accumulates per-classification
counters on the run summary.

# PORT-SEAM: private lazy try/except imports of scoring_orchestrator /
# data_enricher (graceful degradation when a downstream component is
# absent) become the ScanServices fail-open `is None` skip idiom -- same
# pattern as jobcannon.engine.ats_scanner._run._score_new_ats_jobs, which
# this module's logic is otherwise a near-exact duplicate of (both port
# the same private v3.0 scoring-trigger shape).
"""

from __future__ import annotations

import logging

from jobcannon.engine.classification import derive_classification
from jobcannon.engine.services import get_services  # PORT-SEAM: seam (L-0443)

logger = logging.getLogger(__name__)


def _score_new_jobs(
    # PORT-SEAM: db_path param dropped -- svc.connection_factory() is zero-arg (L-0443)
    config: dict,
    new_job_keys: list[str],
    summary: dict,
) -> None:
    """Score newly discovered jobs via the unified v3.0 scorer.

    v3.0 (Phase 34 Plan 3 Commit A): routes through score_and_persist_job so the
    `classification` column populates on every scored row; per-classification
    counters replace haiku_scored / sonnet_evaluated.
    """
    svc = get_services()  # PORT-SEAM: seam (L-0443)
    if not new_job_keys or svc.score_and_persist_job is None:
        # PORT-SEAM: fail-open -- mirrors the private try/except ImportError
        # graceful-degradation contract (L-0443)
        return

    # 2026-05-17 hotfix Fix 5: dropped the tier_has_configured_provider
    # pre-flight check. After Fix 4, ProviderCascadeExhaustedError is the
    # canonical "no provider" signal and is caught by the orchestrator's
    # per-job try/except at job_scorer.py — same posture as
    # ats_scanner/_run.py. Eliminating the asymmetry removes a class of
    # cascade-bypass regressions.

    serpapi_key = svc.get_secret(
        "sources.serpapi.api_key", config=config
    )  # PORT-SEAM: seam (L-0443)

    with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0443)
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue

                job_row = dict(row)

                # Enrich BEFORE scoring — careers_crawl produces title+URL only
                # shells, so the scorer would otherwise read an empty description.
                if svc.enrich_job is not None and (  # PORT-SEAM: seam (L-0443)
                    not job_row.get("jd_full")
                    or job_row.get("salary_min") is None
                    or not job_row.get("location")
                ):
                    try:
                        enriched = svc.enrich_job(  # PORT-SEAM: seam (L-0443)
                            job_row,
                            serpapi_key=serpapi_key,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            job_row.update(enriched)
                    except Exception as enrich_err:
                        logger.debug(
                            "careers_crawl enrichment failed for '%s' (non-fatal): %s",
                            dedup_key,
                            enrich_err,
                        )

                result = svc.score_and_persist_job(  # PORT-SEAM: seam (L-0443)
                    job_row,
                    conn,
                    config,
                )
                if result is None:
                    continue
                summary["scored"] = summary.get("scored", 0) + 1
                if getattr(result, "status", None) != "ok" or result.data is None:
                    continue
                cls = derive_classification(
                    result.data.sub_scores,
                    job_row.get("legitimacy_note"),
                    degenerate=getattr(result.data, "degenerate", False),
                )
                key = f"classified_{cls}"
                summary[key] = summary.get(key, 0) + 1
            except Exception as e:
                logger.warning(
                    "careers_crawl scoring error for '%s': %s",
                    dedup_key,
                    e,
                    exc_info=True,
                )
