"""The durable Procrastinate worker (spec §4, OD-2 end-of-1B standup).

Startup owns BOTH schema authorities (Global Constraints: Two-Schema-
Authorities ruling): our applied-set ledger via run_migrations (idempotent by
construction), then procrastinate's own queue schema guarded by a
to_regclass existence probe (apply_schema is plain CREATEs — NOT idempotent;
procrastinate's own `procrastinate migrate` owns its upgrades, a runbook
step when the pin moves). Then the four engine seams, then run_worker —
which opens the async connector itself (do NOT wrap it in app.open()).

Signal handling: run_worker's default install_signal_handlers=True turns
SIGTERM (Render redeploy) into a graceful stop; a job still running when
Render's kill grace expires is reclaimed later by procrastinate's
heartbeat/stalled-worker machinery — do not hand-roll shutdown logic here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# psycopg3's async path cannot run on Windows' default ProactorEventLoop —
# select the selector policy BEFORE any event loop is created. Harmless on
# Linux (Render); required on the self-hosted Windows CI runner, where the
# integration test imports this module.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg

from jobcannon.db.migrate import run_migrations
from jobcannon.host import init_engine_seams, load_host_config
from jobcannon.host import tasks

logger = logging.getLogger(__name__)


def _ensure_procrastinate_schema() -> None:
    """Self-contained: probe via a short-lived plain connection, and only on
    first boot open the app (sync companion) just long enough to apply."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as probe:
        exists = probe.execute("SELECT to_regclass('public.procrastinate_jobs')").fetchone()[0]
    if exists is None:
        logger.info("applying procrastinate schema (first boot)")
        with tasks.app.open():
            tasks.app.schema_manager.apply_schema()


def main() -> None:
    logging.basicConfig(level=os.environ.get("JC_LOG_LEVEL", "INFO"))
    host_config = load_host_config()
    run_migrations(host_config.database_url)
    init_engine_seams(host_config)
    _ensure_procrastinate_schema()
    tasks.app.run_worker(
        queues=["scan", "maintenance"],
        concurrency=int(os.environ.get("JC_WORKER_CONCURRENCY", "2")),
    )


if __name__ == "__main__":
    main()
