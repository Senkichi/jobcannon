"""sync_bp — `POST /sync/now`: on-demand IMAP ingestion for the current user.

This is the host-seam + queue-endpoint half of L-0056's HOLD trigger
(`scheduler/_sync.py`, private repo's manual "Sync Now" scheduler job) --
see design-aggregators-imap.md §3, "L-0056 trigger satisfied." Landing this
route satisfies that trigger without porting `_sync.py` itself: the trigger
was "on-demand ingestion orchestration behind a host seam + a queue
endpoint," and deferring `jobcannon.host.ingestion_tasks.run_user_ingest`
by name IS that seam.

Not listed in `jobcannon.web.PUBLIC_PATHS`, so the `before_request` gate in
jobcannon/web/__init__.py already 401s an unauthenticated request.

Web import boundary (load-bearing, design note §3): this module MUST NEVER
import `jobcannon.host.ingestion_tasks` or `imapclient` -- `task_app.py`'s
docstring forbids the web process from pulling in the heavy task stack.
`jobcannon/host/user_deletion.py::cascade_delete_user` is the established
precedent this route copies exactly: defer BY DOTTED-NAME STRING through
the light `task_app.app` object (`configure_task(name, allow_unknown=True,
queue=...)`), with the same `AppNotOpen` -> `ensure_open()` -> retry-once
-> generic-`Exception`-logs-not-raises sequence. `INGEST_TASK_NAME` /
`_INGEST_QUEUE` are re-declared as local literals here rather than imported
from `ingestion_tasks.py`, for that same reason -- mirrors
`user_deletion.py`'s own `_PURGE_POSTHOG_PERSON_QUEUE` local literal.
`tests/host/test_sync.py::test_ingest_task_name_matches_a_registered_worker_task`
pins this string against the real `jobcannon.host.tasks`-style registry (via
a worker-process-only import, test-only) so a typo here can't silently defer
jobs no worker will ever run.

Feature-gated (design note §3: "the periodic tick and the on-demand route
both ship behind the flag") via
`jobcannon.host.config.imap_ingest_enabled()` -- the same env-read helper
`jobcannon.host.ingestion_tasks.enqueue_imap_ingest` uses, so the parsing
rule lives in exactly one place. Disabled reads as 404, not 403: this
mirrors treating an unshipped feature as though the route doesn't exist,
rather than revealing its presence to an unauthorized-feeling but
authenticated caller.
"""

from __future__ import annotations

import logging

from flask import Blueprint, abort, g, jsonify

from jobcannon.host.config import imap_ingest_enabled

logger = logging.getLogger(__name__)

sync_bp = Blueprint("sync", __name__)

# The fully-qualified dotted name of jobcannon.host.ingestion_tasks.run_user_ingest
# -- matches procrastinate's default task-name derivation (<module>.<function>).
# See module docstring above for why this is a local literal, not an import.
INGEST_TASK_NAME = "jobcannon.host.ingestion_tasks.run_user_ingest"

# Must match jobcannon.host.ingestion_tasks.run_user_ingest's own
# @app.task(queue=INGEST_QUEUE) ("ingest") -- configure_task's
# allow_unknown fallback defaults an unspecified queue to procrastinate's
# DEFAULT_QUEUE ("default"), which jobcannon/worker/__main__.py's
# run_worker(queues=[...]) would never poll for a wrongly-queued job.
_INGEST_QUEUE = "ingest"


@sync_bp.post("/sync/now", strict_slashes=False)
def post_sync_now():
    if not imap_ingest_enabled():
        abort(404)

    from procrastinate import exceptions as procrastinate_exceptions

    from jobcannon.host import task_app

    user_id = g.clerk_user.user_id
    deferrer = task_app.app.configure_task(
        INGEST_TASK_NAME,
        allow_unknown=True,
        queue=_INGEST_QUEUE,
        queueing_lock=f"ingest:{user_id}",
    )
    try:
        deferrer.defer(user_id=user_id)
    except procrastinate_exceptions.AppNotOpen:
        # See jobcannon.host.user_deletion.cascade_delete_user's identical
        # branch: the web process never opens task_app.app's connector
        # eagerly, so lazily open it now and retry exactly once. If
        # ensure_open() itself can't heal this, it raises AppNotOpen
        # uncaught -- a real wiring bug that must surface loudly.
        task_app.ensure_open()
        try:
            deferrer.defer(user_id=user_id)
        except procrastinate_exceptions.AlreadyEnqueued:
            return jsonify({"status": "already_queued"}), 200
    except procrastinate_exceptions.AlreadyEnqueued:
        return jsonify({"status": "already_queued"}), 200
    except Exception:
        logger.exception("post_sync_now: failed to enqueue IMAP ingest for the current user")
        return jsonify({"status": "error"}), 500

    return jsonify({"status": "queued"}), 202
