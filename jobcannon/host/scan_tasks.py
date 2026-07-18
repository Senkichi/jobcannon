"""Host entry wrappers around the engine scan. Assume the three engine seams
are already wired by the caller (scripts/run_scan_once.py or PR-10's worker)."""

from __future__ import annotations

from typing import Any

from jobcannon.db import connection_factory
from jobcannon.engine.ats_scanner import run_ats_scan
from jobcannon.host.structural_axes import score_pending_structural_axes


def run_scan_task(company_names: list[str] | None = None) -> dict[str, Any]:
    """Drive one ATS scan, then compute structural axes for any postings not
    yet scored under the current rules version. db_path is vestigial under the
    Postgres adapter (connection_factory ignores it); pass a placeholder.

    The structural tail runs AFTER run_ats_scan has committed its postings. A
    failure here propagates (the scan's own writes are already durable, and
    score_pending_structural_axes is an idempotent versioned re-sweep — a task
    retry re-scores only the still-pending rows), so we surface it rather than
    swallow a scoring outage."""
    config = _runtime_config()
    summary = run_ats_scan("__hosted__", config, company_names=company_names)
    with connection_factory() as conn:
        structural_scored = score_pending_structural_axes(conn, config)
    return {**summary, "structural_axes_scored": structural_scored}


def run_expiry_check_task() -> None:
    """RESERVED (spec taxonomy). Real expiry reconciliation is a multi-tenant
    redesign, not a port: the engine's expiry logic gates on per-user
    pipeline_status and writes pipeline_events, neither of which exists as a
    shared-corpus concept in m0001. Deferred until that redesign is ticketed."""
    raise NotImplementedError("expiry-check is reserved; see module docstring")


def run_stale_detect_task() -> None:
    """RESERVED (spec taxonomy). stale_detector.run_stale_detection is not invoked
    by run_ats_scan and its ported logic gates on per-user pipeline_status;
    deferred with expiry-check above."""
    raise NotImplementedError("stale-detect is reserved; see module docstring")


def _runtime_config() -> dict[str, Any]:
    from jobcannon.engine import runtime_config

    return runtime_config.get_runtime_config()
