"""The single user-deletion cascade path (issues #135, #136).

`cascade_delete_user` is the ONE place that erases a user: both the
`user.deleted` Clerk webhook (`jobcannon/web/webhooks.py`) and issue #136's
reconciliation sweep (`reconcile_deleted_users`, below) call into this same
function rather than each running their own copy — a second, drifted
deletion path is exactly the bug class this module exists to prevent.

Deliberately a LIGHT module (only imports `jobcannon.db._users`,
`jobcannon.host.posthog_client`, and `jobcannon.host.task_app` — none of
which pull in the ATS-scanning/fastembed stack; see task_app.py's
docstring): `jobcannon/web/webhooks.py` calls `cascade_delete_user`
directly from the web process, so this module must stay safe to import
there. The actual PostHog HTTP call (issue #135) never happens here — it is
DEFERRED to `jobcannon.host.tasks.purge_posthog_person`, an async
procrastinate task on the worker's `maintenance` queue, via
`task_app.app.configure_task(...)` (deferring by dotted-name string, not by
importing `jobcannon.host.tasks` itself). That keeps a PostHog outage from
ever blocking or delaying the local deletion, which has already committed
by the time the purge task even runs.

`run_reconciliation_sweep` (issue #136) is the periodic catch-net for a
`user.deleted` webhook Clerk never delivered (or delivered while this app
was down, outside Svix's retry window): for old, non-anon `users` rows, it
asks Clerk's Backend API directly whether the user still exists. A
DEFINITIVE 404 runs `cascade_delete_user`; every other outcome (the user is
still present, a non-404 error status, a network/SDK exception) fails
CLOSED — no deletion. It NEVER issues a Clerk delete itself, only reads.
"""

from __future__ import annotations

import logging
import time

from procrastinate import exceptions as procrastinate_exceptions

logger = logging.getLogger(__name__)

# Circuit breaker (HIGH-3): a valid CLERK_SECRET_KEY for the WRONG Clerk
# instance authenticates fine (no 401) but 404s for every real user --
# without a batch-level guard, one sweep would hard-delete every checked
# user. Only arms above a minimum sample size: a small user base can
# legitimately have every checked row genuinely gone (a real all-404 sweep
# on 1-4 rows is plausible, not just a misconfiguration signal), so a floor
# avoids treating a correct small sweep as a misconfigured one.
_CIRCUIT_BREAKER_MIN_CHECKED = 5
_CIRCUIT_BREAKER_NOT_FOUND_FRACTION = 0.5

# The fully-qualified dotted name of jobcannon.host.tasks.purge_posthog_person
# -- matches procrastinate's default task-name derivation (<module>.<function>,
# verified against procrastinate 3.9.0). Deferring by this name string (via
# task_app.app.configure_task) never requires jobcannon.host.tasks itself to
# be imported in this process -- see task_app.py's docstring. A test
# (tests/host/test_user_deletion.py) pins that this constant's value actually
# matches a real key in jobcannon.host.tasks.app.tasks, so a typo here can't
# silently defer jobs no worker will ever execute.
PURGE_POSTHOG_PERSON_TASK = "jobcannon.host.tasks.purge_posthog_person"

# Must match jobcannon.host.tasks.purge_posthog_person's own
# @app.task(queue="maintenance") -- configure_task's allow_unknown fallback
# defaults an unspecified queue to procrastinate's DEFAULT_QUEUE ("default"),
# which jobcannon/worker/__main__.py's run_worker(queues=["scan",
# "maintenance"]) never polls. A job deferred with the wrong queue sits
# forever, with no error anywhere -- this constant exists so that fact is
# named once, not re-typed at each defer call site.
_PURGE_POSTHOG_PERSON_QUEUE = "maintenance"

# Pacing between per-row Clerk Backend API lookups in the reconciliation
# sweep (issue #136: "rate-limit-aware"). Not derived from Clerk's published
# rate limits (no single number is authoritative across endpoints/plans);
# this is a conservative, deliberately-slow default -- 50 rows at 0.25s
# apart is ~12.5s of sweep wall-clock, negligible against the daily cron
# cadence.
_CLERK_LOOKUP_DELAY_S = 0.25


def cascade_delete_user(conn, user_id: str) -> None:
    """The one deletion path (see module docstring). Hard-deletes the
    `users` row (cascading to every child table via FK, same as always),
    then -- if analytics pseudonymization is configured -- defers a
    PostHog person purge for the user's pseudonym. Never receives or logs
    the raw user_id in any log line below (pseudonym only)."""
    from jobcannon.db._users import delete_user
    from jobcannon.host import posthog_client, task_app

    pseudonym = posthog_client.pseudonymize(user_id)
    delete_user(conn, user_id)
    if pseudonym is None:
        return
    deferrer = task_app.app.configure_task(
        PURGE_POSTHOG_PERSON_TASK,
        allow_unknown=True,
        queue=_PURGE_POSTHOG_PERSON_QUEUE,
    )
    try:
        deferrer.defer(distinct_id=pseudonym)
    except procrastinate_exceptions.AppNotOpen:
        # HIGH-1: the web process never opens task_app.app's own connector
        # eagerly (see task_app.py's docstring for why) -- lazily open it
        # now, on first need, and retry exactly once. If ensure_open()
        # itself can't heal this (no DATABASE_URL configured -- wiring
        # genuinely never ran in this process), it raises AppNotOpen
        # itself, which propagates from here uncaught: that is a real
        # wiring bug and must surface loudly, not be silently swallowed a
        # second time (the original bug this fixes).
        task_app.ensure_open()
        deferrer.defer(distinct_id=pseudonym)
    except Exception:
        # A genuine transient failure enqueueing the purge (e.g. a DB
        # hiccup on the INSERT into procrastinate_jobs) -- the user's own
        # deletion has already committed above, so this logs rather than
        # raising and turning a successful deletion into a 500 (which would
        # make Clerk retry the whole webhook delivery).
        logger.exception(
            "cascade_delete_user: failed to enqueue PostHog purge for a "
            "deleted user's pseudonym -- deletion itself succeeded"
        )


def _lookup_clerk_user(clerk_users, user_id: str) -> str:
    """Returns "present" | "not_found" | "rate_limited" | "error". Only a
    DEFINITIVE 404 from Clerk's Backend API is "not_found" -- `models.
    ClerkErrors` carries the real HTTP status (verified against
    clerk_backend_api's SDK: `Users.get` raises `ClerkErrors` with an
    inherited `.status_code` on 4xx SDK-recognized errors, `SDKError` -- no
    reliable status code -- on other failures). A 429 is broken out as its
    own "rate_limited" outcome (issue #136: "rate-limit-aware") so the sweep
    can back off/abort early instead of hammering Clerk row after row.
    Every other status and every exception fails closed ("error"), per
    issue #136's design: no deletion, logged, retried next sweep.

    Mirrors `jobcannon.web.auth._clerk_failure_reason`'s documented
    invariant in every log line below: never reads `str(exc)`/`exc.args` --
    only the status code and the exception's own class name -- so nothing
    from the request (headers, including the CLERK_SECRET_KEY bearer token,
    or a Clerk user id embedded in a request URL) can end up in a log
    line."""
    from clerk_backend_api import models

    try:
        clerk_users.get(user_id=user_id)
    except models.ClerkErrors as exc:
        if exc.status_code == 404:
            return "not_found"
        if exc.status_code == 429:
            logger.warning(
                "reconcile_deleted_users: Clerk rate-limited (429) a "
                "candidate lookup -- backing off"
            )
            return "rate_limited"
        logger.warning(
            "reconcile_deleted_users: Clerk returned status %s for a "
            "candidate row (not a definitive 404) -- leaving it undeleted "
            "for retry next sweep",
            exc.status_code,
        )
        return "error"
    except models.SDKError as exc:
        logger.warning(
            "reconcile_deleted_users: Clerk SDK error (%s) looking up a candidate row",
            type(exc).__name__,
        )
        return "error"
    except Exception as exc:
        logger.warning(
            "reconcile_deleted_users: unexpected error (%s) looking up a candidate row",
            type(exc).__name__,
        )
        return "error"
    return "present"


def run_reconciliation_sweep(
    connection_factory,
    clerk_users,
    *,
    settle_days: int,
    row_cap: int,
    sleep_fn=time.sleep,
) -> dict:
    """Issue #136's reconciliation sweep. `connection_factory` is a
    callable (mirrors jobcannon.db.connection_factory), not a single
    already-open connection: this sweep spans multiple independent DB
    operations separated by rate-limit sleeps, and holding one pooled
    connection idle across `row_cap * _CLERK_LOOKUP_DELAY_S` seconds
    (~12.5s at defaults) would tie up a connection-pool slot on a single-
    concurrency worker for no reason -- jobcannon.host.events.log_event's
    per-operation `with connection_factory() as conn:` is the same idiom
    this mirrors. `clerk_users` is a Clerk SDK `users` resource (e.g.
    `build_clerk_client(host_config).users`), injected so tests can pass a
    fake with no network.

    Returns {"status": S, "checked": N, "deleted": N, "confirmed_present": N,
    "errors": N} -- always, even when N == 0 for everything (a caller
    logging this at INFO unconditionally is how an operator tells "the
    sweep ran and found nothing to do" apart from "the sweep never ran" --
    see jobcannon.host.tasks.reconcile_deleted_users). `status` is "ok"
    unless the circuit breaker below tripped ("clerk_misconfigured", zero
    deletions performed) or every checked row errored
    ("degraded", HIGH-3 / F6 / VERIFIED-4).

    Outcomes are collected for ALL candidates BEFORE any deletion happens
    (HIGH-3): a valid CLERK_SECRET_KEY for the WRONG Clerk instance
    authenticates fine (no 401) but returns a definitive 404 for every real
    user, which -- without this guard -- would hard-delete the whole
    candidate cohort in one sweep. `_CIRCUIT_BREAKER_MIN_CHECKED` is a floor
    below which the breaker never arms: a genuinely small user base can
    legitimately have every checked row turn out gone, so treating an
    all-404 result on 1-4 rows as a misconfiguration would block real
    deletions on a small install."""
    from jobcannon.db._users import (
        list_users_pending_deletion_reconciliation,
        mark_deletion_checked,
    )

    with connection_factory() as conn:
        candidate_ids = list_users_pending_deletion_reconciliation(
            conn, settle_days=settle_days, limit=row_cap
        )

    outcomes: list[tuple[str, str]] = []
    rate_limited_abort = False
    for i, user_id in enumerate(candidate_ids):
        if i:
            sleep_fn(_CLERK_LOOKUP_DELAY_S)
        outcome = _lookup_clerk_user(clerk_users, user_id)
        outcomes.append((user_id, outcome))
        if outcome == "rate_limited":
            rate_limited_abort = True
            logger.error(
                "reconcile_deleted_users: Clerk rate-limited this sweep -- "
                "aborting the remaining %d candidate(s) this tick",
                len(candidate_ids) - len(outcomes),
            )
            break

    checked = len(outcomes)
    not_found = sum(1 for _, outcome in outcomes if outcome == "not_found")
    if checked >= _CIRCUIT_BREAKER_MIN_CHECKED and (
        not_found == checked or not_found / checked > _CIRCUIT_BREAKER_NOT_FOUND_FRACTION
    ):
        logger.error(
            "reconcile_deleted_users: %d/%d candidates came back a "
            "definitive 404 from Clerk this sweep -- refusing to delete "
            "ANY of them (this looks like CLERK_SECRET_KEY pointing at the "
            "wrong Clerk instance, not a genuine mass account deletion)",
            not_found,
            checked,
        )
        return {
            "status": "clerk_misconfigured",
            "checked": checked,
            "deleted": 0,
            "confirmed_present": 0,
            "errors": 0,
        }

    deleted = confirmed_present = errors = 0
    for user_id, outcome in outcomes:
        if outcome == "not_found":
            with connection_factory() as conn:
                cascade_delete_user(conn, user_id)
            deleted += 1
        elif outcome == "present":
            with connection_factory() as conn:
                mark_deletion_checked(conn, user_id)
            confirmed_present += 1
        else:  # "error" or "rate_limited" -- see mark_deletion_checked's
            # docstring for why an errored row is stamped too (MEDIUM-5 /
            # FINDING-4: an un-stamped error row starves the rotation).
            errors += 1
            with connection_factory() as conn:
                mark_deletion_checked(conn, user_id)

    # Either every checked row errored, or the sweep was cut short by a
    # rate-limit abort (issue #136 "rate-limit-aware", HIGH-3-adjacent) --
    # both leave part of the candidate cohort unexamined, so both must read
    # as something other than a healthy "ok" tick.
    status = "degraded" if (checked > 0 and errors == checked) or rate_limited_abort else "ok"
    return {
        "status": status,
        "checked": checked,
        "deleted": deleted,
        "confirmed_present": confirmed_present,
        "errors": errors,
    }
