"""Host entry wrappers around the engine scan. Assume the three engine seams
are already wired by the caller (scripts/run_scan_once.py or PR-10's worker)."""

from __future__ import annotations

import logging
from typing import Any

from jobcannon.db import connection_factory
from jobcannon.engine.ats_scanner import run_ats_scan
from jobcannon.host.embeddings import embed_pending_postings
from jobcannon.host.structural_axes import score_pending_structural_axes

logger = logging.getLogger(__name__)


def run_scan_task(company_names: list[str] | None = None) -> dict[str, Any]:
    """Drive one ATS scan, then compute structural axes AND JD embeddings for
    any postings not yet processed under the current versions.

    The structural tail runs AFTER run_ats_scan has committed its postings and
    PROPAGATES on failure (pure Python — a failure there is a bug, and its
    idempotent versioned re-sweep means a task retry re-scores only still-
    pending rows). The embed tail (_embed_pending_best_effort) is BEST-EFFORT:
    embedding pulls a heavy native runtime (fastembed/onnxruntime) plus a
    first-run model download and no consumer reads embeddings yet, so an
    embedding-infra hiccup must not fail an otherwise-successful scan — it is
    logged and surfaced as postings_embedded=None AND embedding_error=<message>,
    and the versioned re-sweep re-embeds the still-pending rows next scan.
    """
    config = _runtime_config()
    summary = run_ats_scan("__hosted__", config, company_names=company_names)
    with connection_factory() as conn:
        structural_scored = score_pending_structural_axes(conn, config)
        embedded, embedding_error = _embed_pending_best_effort(conn, config)
    return {
        **summary,
        "structural_axes_scored": structural_scored,
        "postings_embedded": embedded,
        "embedding_error": embedding_error,
    }


def _embed_pending_best_effort(conn: Any, config: Any) -> tuple[int | None, str | None]:
    """Run the embed tail, swallowing (but loudly logging) any failure so an
    embedding-infra hiccup never fails a successful scan. Returns (count, None)
    on success, or (None, message) if the tail errored — the message is threaded
    into the run summary as `embedding_error` so the swallow is observable to
    monitoring, not silent, and the versioned re-sweep retries the still-pending
    rows next scan. The connection is returned to the pool afterward and reset
    before reuse; each per-row write is individually transactional (rolled back
    on error), so a swallowed failure leaves no half-applied write behind."""
    try:
        return embed_pending_postings(conn, config), None
    except Exception as exc:
        logger.exception("embedding tail failed; postings remain pending for the next scan")
        return None, str(exc)


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
