"""Host entry wrappers around the engine scan. Assume the three engine seams
are already wired by the caller (scripts/run_scan_once.py or PR-10's worker)."""

from __future__ import annotations

from typing import Any

from jobcannon.engine.ats_scanner import run_ats_scan


def run_scan_task(company_names: list[str] | None = None) -> dict[str, Any]:
    """Drive one ATS scan. db_path is vestigial under the Postgres adapter
    (connection_factory ignores it); pass a placeholder."""
    return run_ats_scan("__hosted__", _runtime_config(), company_names=company_names)


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
