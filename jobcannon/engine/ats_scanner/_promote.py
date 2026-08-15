"""Source-URL based ATS promotion for miss/error/pending companies.

Uses centralized ``reconcile_company_ats`` (Phase B batch reconciliation).
Aggregates per-job ``source_urls`` with precedence ranking, verifies with live API
calls, writes audited evidence columns — never trusts URL shape alone.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
"""

import logging

from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)


def promote_ats_from_source_urls(db_path: str, config: dict) -> dict:
    """Backward-compatible facade for nightly scheduler.

    Processes up to ``ats.identity_reconcile.max_companies_per_promote_run``.
    Includes ``pending`` companies with ATS URLs (Phase B backlog drain).

    Thread-safe via internal ``standalone_connection`` per batch iteration.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (JF_CONFIG snapshot).

    Returns:
        Counts keyed by outcome (``checked``, ``promoted``, failures, skips).
        A host that leaves ``ScanServices.promote_ats_scheduler_batch`` unset
        skips the reconcile/promotion step entirely (fail-closed — no
        promotion without the identity-verified reconciliation machinery).
    """
    svc = get_services()
    if svc.promote_ats_scheduler_batch is None:
        logger.info(
            "promote_ats_from_source_urls: no promote_ats_scheduler_batch service configured — skip"
        )
        return {"checked": 0, "promoted": 0, "skipped": "promote_ats_scheduler_batch_unavailable"}
    summary = svc.promote_ats_scheduler_batch(db_path, config)
    logger.info(
        "promote_ats_from_source_urls summary: %s",
        summary,
    )
    return summary
