"""PORTED (persistence logic) from job_finder/web/ingestion_runner.py
@ bc30befa311b5c78868ece3dddd60b44d018f444 (private job-cannon). Ledger
L-0188.

# PORT-SEAM: this is a PARTIAL/function-level port, not a whole-file one --
# private's ingestion_runner.py is ~1300 lines driving FOUR aggregator
# lanes (IMAP, SerpAPI, DataForSEO, portal-search) through a shared
# `_run_simple_source` dispatcher; this PR (design-aggregators-imap.md §3,
# PR-4) is IMAP-lane-only, so only the two functions that turn parsed
# `Job` objects into `postings` rows are carried: `_score_and_persist`
# (scoring itself, out of scope here, and per-source funnel tracking are
# BOTH dropped -- see this PR's Modularity note) and `_upsert_job_company`
# (private's two-step insert-then-`UPDATE jobs SET company_id` company
# resolution is replaced by the one-step resolve-company-first pattern
# jobcannon.engine.careers_crawler._persistence._upsert_and_log already
# established: `svc.upsert_company(...)` runs BEFORE `svc.upsert_job(...,
# company_id=company_id)`, activating upsert_job's `(company_id,
# source_id)` secondary dedup match at insert time -- private's flow never
# reached that path because company_id didn't exist yet at insert time).
# See scripts/port_fidelity_diff.py's output for this row: it is run
# against the two lifted functions, not the whole private file, following
# PR #353's established precedent for this exact scope mismatch.
#
# Everything else in this module -- the procrastinate `@app.task`/
# `@app.periodic` declarations, the `ingest` queue, the RLS-safe
# consented-user query, and the L-0111 vendor_account_error wrap around
# `run_imap_intake` -- is NOT a port: there is no procrastinate (or any
# task queue) in the single-user private repo, so this scaffolding has no
# private-repo equivalent. It mirrors jobcannon/host/scan_tasks.py's
# "host module, direct DB access, no IngestionServices DI seam" shape
# (design-aggregators-imap.md §1.8 explicitly declines inventing one) and
# jobcannon/host/tasks.py's enqueue_due_scans periodic-tick pattern.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from procrastinate import exceptions as procrastinate_exceptions

from jobcannon.engine.ats_detection import extract_ats_from_urls
from jobcannon.engine.parsed_job import DenylistedCompanyError, ListingTileError, ParsedJob
from jobcannon.engine.services import get_services
from jobcannon.host.task_app import app

logger = logging.getLogger(__name__)

INGEST_QUEUE = "ingest"


class _NoVendorAccountError(Exception):
    """Placeholder exception type used when ScanServices.vendor_account_error
    is None (no host has wired a real VendorAccountError type in). It simply
    never matches any real exception raised below, so the `except
    svc.vendor_account_error or _NoVendorAccountError` clause degrades to a
    no-op catch instead of raising TypeError on `except None`. Mirrors
    jobcannon/engine/data_enricher.py's identical idiom (L-0111)."""


def _persist_jobs(conn: Any, jobs: list) -> dict:
    """Turn parsed IMAP `Job` objects into `postings` rows.

    # PORT-SEAM: mirrors private's _score_and_persist (ingestion_runner.py
    # @ bc30befa311, lines ~1150-1230) with scoring and per-source funnel
    # tracking removed (out of PR-4's stated scope -- "task + wiring" only;
    # both are Modularity-note follow-up candidates, not silent drops) and
    # company resolution replaced by careers_crawler's _upsert_and_log
    # pattern (see module docstring above). The per-job broad
    # `except Exception` below is that same precedent's convention: one bad
    # job (a rejected company name, a malformed URL) must not lose the rest
    # of the batch.
    """
    svc = get_services()
    summary: dict = {
        "jobs_found": len(jobs),
        "jobs_new": 0,
        "jobs_updated": 0,
        "jobs_touched": 0,
        "jobs_unchanged": 0,
        "job_errors": [],
    }

    for job in jobs:
        try:
            try:
                parsed = ParsedJob.from_job(job)
            except (DenylistedCompanyError, ListingTileError):
                # Denylisted company (I-10) or result-count tile (I-14):
                # both are hard drops -- skip silently, matching
                # careers_crawler._upsert_and_log's identical handling.
                continue

            ats_platform, ats_slug = extract_ats_from_urls(
                [job.source_url] if job.source_url else []
            )
            company_id = svc.upsert_company(
                conn, job.company, ats_platform=ats_platform, ats_slug=ats_slug
            )
            result = svc.upsert_job(conn, parsed, company_id=company_id)

            if result.kind == "inserted":
                summary["jobs_new"] += 1
            elif result.kind == "updated":
                summary["jobs_updated"] += 1
            elif result.kind == "touched":
                summary["jobs_touched"] += 1
            else:
                summary["jobs_unchanged"] += 1
        except Exception as job_err:
            error_msg = f"{getattr(job, 'company', '<unknown>')} job error: {job_err}"
            summary["job_errors"].append(error_msg)
            logger.warning("ingestion_tasks: %s", error_msg)

    return summary


def run_user_ingest_task(user_id: str) -> dict:
    """Fetch this tenant's IMAP job-alert emails and persist them.

    Wraps run_imap_intake's fetch in the L-0111 vendor_account_error seam
    (design note §5): IMAP never actually raises VendorAccountError in
    practice (jobcannon/host/ingestion/imap_intake.py's own module
    docstring, lines 63-72, confirms this lane's gate has no call site of
    its own and belongs here instead) -- the except clause is structurally
    present, currently inert, matching data_enricher.py's established
    idiom. No ScanServices-shaped IngestionServices DI object is invented
    (design note §1.8 declines one); `get_services()` is called directly.

    Known limitation (inherited, not fixed here -- PR #353's contract):
    run_imap_intake COMMITS capture.record_run and
    _mailbox_credentials.advance_uid_highwater internally BEFORE returning
    jobs to this function. If _persist_jobs below fails after that point,
    the UID watermark has already advanced -- those specific parsed jobs
    are lost; the next run will not re-fetch their UIDs. Reopening that
    transaction boundary is out of this unit's scope (see this PR's
    Modularity note).
    """
    from jobcannon.host.credentials import build_mailbox_resolver
    from jobcannon.host.ingestion.imap_intake import run_imap_intake

    svc = get_services()
    with svc.connection_factory() as conn:
        resolver = build_mailbox_resolver(conn, user_id)
        try:
            result = run_imap_intake(conn, user_id, resolver=resolver)
        except svc.vendor_account_error or _NoVendorAccountError as e:
            logger.warning(
                "run_user_ingest_task: vendor account error for user_id=%s: %s", user_id, e
            )
            return {"status": "vendor_account_error", "jobs_found": 0}

        if not result.jobs:
            return {"status": "ok", "jobs_found": 0, "processed_uids": len(result.processed_uids)}

        persisted = _persist_jobs(conn, result.jobs)

    return {"status": "ok", "processed_uids": len(result.processed_uids), **persisted}


@app.task(queue=INGEST_QUEUE)
def run_user_ingest(user_id: str) -> dict:
    return run_user_ingest_task(user_id)


def _tick_connection():
    """Seam for tests: the tick's DB context. Production: the pooled
    factory. Mirrors jobcannon/host/tasks.py's own `_tick_connection`
    helper -- a per-module private copy since this periodic lives in a
    sibling module, same `app` instance."""
    from jobcannon.db import connection_factory

    return connection_factory()


def _consented_user_ids(conn: Any) -> list[str]:
    """Users eligible for a periodic IMAP-ingest enqueue tick.

    # PORT-SEAM: deliberate deviation from design-aggregators-imap.md §3's
    # literal predicate (`mailbox_consent AND EXISTS(active
    # mailbox_credentials)`). `mailbox_credentials` carries `ENABLE + FORCE
    # ROW LEVEL SECURITY` with a single `tenant_isolation` policy keyed on
    # `current_setting('app.user_id', true) = user_id` (m0025). A
    # cross-tenant EXISTS(...) join against that table with no
    # `app.user_id` session var set does not error and does not
    # over-enqueue -- FORCE RLS makes the policy predicate evaluate to
    # always-false for every row, so the join silently matches NOBODY,
    # enqueuing zero users regardless of how many are actually eligible.
    # That is worse than jobcannon/host/scan_tasks.py::_due_company_names's
    # established "cheap approximation, over-enqueueing is safe" pattern:
    # an empty result here isn't safely-over-inclusive, it's silently
    # broken. tests/host/test_ingestion_tasks.py::
    # test_naive_cross_tenant_mailbox_join_returns_zero_rows_under_rls is
    # the positive control proving this empirically (0 rows with no
    # set_config, 1 row after set_config'ing the owning tenant, run as a
    # non-superuser role -- see that test's docstring for why the role
    # matters).
    #
    # `users` itself carries NO row-level security (confirmed by grepping
    # every migration under jobcannon/db/migrations/ for `mailbox_credentials`
    # / `byo_key_credentials` -- the only two RLS-protected tables), so this
    # plain SELECT is safe under any role, no session var required. The
    # cost of over-enqueueing a consented-but-credential-less user is one
    # cheap task: run_imap_intake's own `progress is None` branch
    # (jobcannon/host/ingestion/imap_intake.py:242-249) already logs and
    # no-ops rather than touching the network, and the per-user
    # queueing_lock below dedupes repeat ticks -- the same "authority stays
    # downstream" shape _due_company_names documents.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute("SELECT id FROM users WHERE mailbox_consent").fetchall()
    return [r["id"] for r in rows]


@app.periodic(
    cron=os.environ.get("JC_IMAP_INGEST_CRON", "0 */8 * * *"),
    periodic_id="enqueue_imap_ingest",
)
@app.task(queue=INGEST_QUEUE, queueing_lock="enqueue_imap_ingest")
def enqueue_imap_ingest(timestamp: int) -> dict:
    """Periodic tick (design note §3, 3x/day cadence mirroring private's
    schedule): enqueues one `run_user_ingest` per mailbox_consent'd user.

    Reads IMAP_INGEST_ENABLED directly via
    jobcannon.host.config.imap_ingest_enabled() rather than through
    load_host_config() (which raises RuntimeError if DATABASE_URL is
    unset) -- matching JC_EVENTS_RETENTION_DAYS's own os.environ-direct
    read in jobcannon/host/tasks.py, and specifically avoiding a
    load_host_config() call inside a decorator-registered function body:
    tests/host/test_render_config.py::
    test_worker_start_command_module_is_importable imports
    jobcannon.worker.__main__ (which imports this module for decorator
    registration) without DATABASE_URL set.
    """
    from jobcannon.host.config import imap_ingest_enabled

    if not imap_ingest_enabled():
        return {"status": "disabled"}

    with _tick_connection() as conn:
        user_ids = _consented_user_ids(conn)

    enqueued = already = 0
    for user_id in user_ids:
        try:
            run_user_ingest.configure(queueing_lock=f"ingest:{user_id}").defer(user_id=user_id)
            enqueued += 1
        except procrastinate_exceptions.AlreadyEnqueued:
            already += 1
    return {"enqueued": enqueued, "already_enqueued": already}
