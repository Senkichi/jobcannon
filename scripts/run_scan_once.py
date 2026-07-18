"""Hand-triggered single ATS scan against the hosted Postgres corpus.

Run once during a build window:  uv run python -m scripts.run_scan_once
Wires the three engine seams via jobcannon.host.wiring, then runs one scan.

Deviation from the PR draft: the real Wave-1 `init_engine_seams(host_config)`
(jobcannon/host/wiring.py) takes a HostConfig and opens the connection pool
itself (`pool_mod.open_pool(host_config.database_url)`); there is no
zero-arg `init_engine_seams()` and no separate `get_pool()`/`close_pool()`
call needed here — `teardown_engine_seams()` closes the pool it opened.
"""

from __future__ import annotations

import logging
import sys

from jobcannon.host.config import load_host_config
from jobcannon.host.scan_tasks import run_scan_task
from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("run_scan_once")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    company_names = argv or None
    host_config = load_host_config()
    init_engine_seams(host_config)
    try:
        summary = run_scan_task(company_names)
        log.info("scan complete: %s", summary)
        return 0
    finally:
        teardown_engine_seams()


if __name__ == "__main__":
    raise SystemExit(main())
