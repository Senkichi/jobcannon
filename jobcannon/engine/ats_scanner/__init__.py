"""ATS (Applicant Tracking System) scanner and company registry package.

The package's first-party concerns live in private sibling modules:

- `_upsert.py`   — `is_company_tracked`: company tracking check.
- `_probe.py`    — `probe_ats_slugs`: speculative ATS-API probing for
                   companies with `ats_probe_status='pending'`.
- `_promote.py`  — `promote_ats_from_source_urls`: source-URL based
                   promotion of miss/error companies.
- `_run.py`      — `run_ats_scan`: scan orchestrator + phase helpers.

Sibling-module re-exports preserve the public API contract used by
tests, scheduler, blueprints/companies, careers_scraper, enrichment_tiers,
and backfill_companies. The package re-exports symbols from
`ats_platforms`, `ats_prober`, and `ats_detection` so callers can keep
importing them from the `ats_scanner` namespace.

`upsert_company` and `reconcile_company_ats` are NOT re-exported here: in
the private source they were static top-level imports; in the engine both
are host-supplied `ScanServices` fields (`svc.upsert_company`,
`svc.reconcile_company_ats`), resolved per-call via `get_services()`
rather than bound at import time (see `services.py`).

Architecture:
- Thread-safe: probe_ats_slugs() and run_ats_scan() open their own
  sqlite3 connections (same pattern as stale_detector.py).
- TESTING guard on probe_ats_slugs and run_ats_scan to prevent external
  API calls in tests.
- Never re-probes cached misses; never downgrades confirmed hits to
  pending (precedence is hit > pending > miss).

ATS URL patterns:
- Lever:        jobs.lever.co/{slug}/...           and api.lever.co/v0/postings/{slug}
- Greenhouse:   boards.greenhouse.io/{slug}/...    and boards-api.greenhouse.io/v1/boards/{slug}
- Ashby:        jobs.ashbyhq.com/{slug}/...        (case-sensitive slug)
"""

from jobcannon.engine.ats_detection import (  # noqa: F401
    ATS_EXTRACTOR_VERSION,
    derive_slug_candidates,
    extract_ats_from_url_best,
    extract_ats_from_urls,
)
from jobcannon.engine.ats_platforms import (  # noqa: F401
    _title_matches,
    scan_ashby,
    scan_greenhouse,
    scan_lever,
)
from jobcannon.engine.ats_prober import (  # noqa: F401
    _BACKOFF_HOURS,
    _MAX_RETRIES,
    _PERMANENT_MISS_CODES,
    _PROBE_STATUS_PRECEDENCE,
    _PROBE_TIMEOUT,
    _TRANSIENT_CODES,
    _compute_retry_after,
    _handle_scan_error,
    _is_transient_error,
    _probe_ashby,
    _probe_greenhouse,
    _probe_lever,
    _probe_lever_with_result,
    _probe_smartrecruiters,
    _probe_workday,
    _reset_retry_state,
    probe_single_company,
)
from jobcannon.engine.ats_scanner._probe import probe_ats_slugs  # noqa: F401

# First-party concerns moved into private sibling modules during S7c.
from jobcannon.engine.ats_scanner._promote import promote_ats_from_source_urls  # noqa: F401
from jobcannon.engine.ats_scanner._run import run_ats_scan  # noqa: F401
from jobcannon.engine.ats_scanner._upsert import is_company_tracked  # noqa: F401
