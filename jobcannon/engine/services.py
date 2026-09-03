"""Dependency-injection seam between the engine and any host application.

The private repo's scan orchestration reached sideways into Flask-side
modules (db_helpers, scoring_orchestrator, data_enricher, pipeline_runner,
careers_scraper, homepage_discoverer, autoheal, ats_company,
ats_identity_reconcile, ats_slug_challenge, config, secrets). The engine
replaces every one of those imports with a field on ScanServices, supplied
by the host at startup via set_services().

Required callables MUST match the signatures of the private-repo functions
they replace (documented per field). Optional hooks default to None; engine
call sites check for None and skip that behavior (the skip semantics per
hook are specified in the Phase 1A plan, Task 3 Step 4).

Identity trio / prober_extensions note: ``identity_reconcile_settings``,
``owner_identity_passes``, and ``resolve_slug_collision`` appear BOTH as
individual fields below (for ``ats_scanner``'s own direct imports of
``ats_identity_reconcile`` / ``ats_slug_challenge``) AND inside the
``prober_extensions`` bundle consumed by ``jobcannon.engine.ats_prober``
(which cannot see ``ScanServices`` directly — it only sees the module-global
set via ``set_prober_extensions``). This is not accidental duplication: a
host constructs these callables once and wires them through both channels
from the same ``ScanServices(...)`` call — the "single wiring site" is
constructing ``ScanServices``, since the scan-orchestration entry point
(``run_ats_scan``) propagates ``services.prober_extensions`` into
``ats_prober.set_prober_extensions()`` automatically (restoring the prior
value in a ``finally``). Hosts that skip ``prober_extensions`` simply get
the fail-closed prober defaults from Task 2's amendment (Step 7e).
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ScanServices:
    # -- persistence (required) --
    connection_factory: Callable[..., AbstractContextManager[sqlite3.Connection]]
    #   replaces db_helpers.standalone_connection(db_path, synchronous="FULL");
    #   host binds db_path, engine enters the context manager. MUST accept an
    #   optional keyword `synchronous: str = "FULL"` — two scan-worker hot-path
    #   call sites pass synchronous="NORMAL" (durability/perf knob) and that
    #   distinction must survive the port.
    upsert_job: Callable[..., Any]
    #   matches job_finder.db._jobs.upsert_job(conn, parsed, *, company_id=None,
    #   score_breakdown=None, ats_platform=None, config=None) -> UpsertResult
    set_jd_full: Callable[..., Any]
    #   matches job_finder.db._jd_full.set_jd_full(conn, dedup_key, text, *,
    #   source, title=None, config=None) -> bool
    upsert_company: Callable[..., Any]
    #   matches job_finder.web.ats_company.upsert_company(conn, name, ...) -> int;
    #   raises CompanyNameRejectedError (name policy) / CompanyUpsertError
    #   (anything else) instead of returning None
    # -- config / secrets (required) --
    config: dict
    get_secret: Callable[..., "str | None"]
    #   matches job_finder.secrets.get_secret(name, *, config=None)
    jd_storage_max_chars: int
    #   replaces job_finder.config.JD_STORAGE_MAX_CHARS
    # -- optional pipeline hooks (None => engine skips that behavior) --
    score_and_persist_job: Callable[..., Any] | None = None
    #   matches scoring_orchestrator.score_and_persist_job(job, conn, config,
    #   scorer_fn=None, *, run_id=None)
    enrich_job: Callable[..., dict] | None = None
    #   matches data_enricher.enrich_job(job_row, serpapi_key=None, conn=None,
    #   config=None, careers_memo=None) -> dict
    run_heal_pass: Callable[[str, dict, list], None] | None = None
    #   matches pipeline_runner._run_heal_pass(db_path, config, degraded_sources)
    find_careers_url: Callable[..., Any] | None = None
    scrape_careers_page: Callable[..., Any] | None = None
    run_homepage_discovery: Callable[..., Any] | None = None
    run_detection: Callable[..., list] | None = None
    #   matches autoheal.health_monitor.run_detection(db_path, config=None)
    identity_reconcile_settings: Callable[..., Any] | None = None
    promote_ats_scheduler_batch: Callable[..., Any] | None = None
    reconcile_company_ats: Callable[..., Any] | None = None
    #   ^ matches ats_identity_reconcile's identity_reconcile_settings /
    #     promote_ats_scheduler_batch / reconcile_company_ats.
    owner_identity_passes: Callable[..., Any] | None = None
    resolve_slug_collision: Callable[..., Any] | None = None
    set_source_id_if_free: Callable[..., Any] | None = None
    #   matches job_finder.db._jobs.set_source_id_if_free(conn, dedup_key,
    #   company_id, source_id) -> None; guarded single-writer for the
    #   (company_id, source_id) partial-unique slot (I-11). None => the
    #   primary-posting merge skips the source_id backfill.
    #   ^ matches ats_slug_challenge's owner_identity_passes /
    #     resolve_slug_collision (the third ats_slug_challenge symbol,
    #     TRIGGER_PREFIX_CAREERS_URL, is a plain string constant, not a
    #     callable, and is copied verbatim into _probe.py instead).
    prober_extensions: Any | None = None
    #   Duck-typed bundle forwarded to jobcannon.engine.ats_prober's
    #   set_prober_extensions() by the scan-orchestration entry point (see
    #   module docstring above and Task 2 amendment Step 7e). Exposes the
    #   nine callables documented on ats_prober._prober_extensions:
    #   promote_from_careers_link, identity_reconcile_settings,
    #   owner_identity_passes, resolve_slug_collision, new_summary,
    #   try_static_extract, try_embedded_json_extract, try_playwright_extract,
    #   upsert_and_log.
    scan_deadline_s: float | None = None
    #   Whole-scan runtime budget in seconds. Replaces the private repo's
    #   job-level max-runtime wall for the ATS scan (the scheduler layer's
    #   _get_job_max_runtime_s) - that layer does not exist hosted,
    #   so the host supplies the wall at its one construction site. None =>
    #   unbounded (dev/CI default; existing construction sites stay valid).
    #   <= 0 is normalized to "no bound" by the engine, matching the
    #   documented ">0" semantics of ats.runtime_limit_s.


_active: ScanServices | None = None


def set_services(services: ScanServices) -> None:
    global _active
    _active = services


def clear_services() -> None:
    global _active
    _active = None


def get_services() -> ScanServices:
    if _active is None:
        raise RuntimeError(
            "jobcannon.engine.services not configured — host must call "
            "set_services(ScanServices(...)) at startup"
        )
    return _active
