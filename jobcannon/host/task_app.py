"""The procrastinate `App` object, extracted out of `jobcannon.host.tasks`
into its own light module (issue #135/#136) so the WEB process can import
`app` — to defer a task by name via `app.configure_task(name,
allow_unknown=True)` — without pulling in `jobcannon.host.tasks`'s
module-level imports.

`jobcannon.host.tasks` imports `from jobcannon.host import scan_tasks`,
which imports `jobcannon.engine.ats_scanner` and
`jobcannon.host.embeddings`/`jobcannon.host.structural_axes` — the whole
ATS-scanning/fastembed/onnxruntime stack. Grepping `jobcannon/web/` confirms
it currently imports none of that; importing `jobcannon.host.tasks` from a
web-process module (e.g. `jobcannon/web/webhooks.py` or
`jobcannon/host/user_deletion.py`, which webhooks.py calls into) would be a
genuine new regression, not a style nit.

`jobcannon.host.tasks` imports `app` from here
(`from jobcannon.host.task_app import app`) rather than constructing its
own — there is exactly one procrastinate App object in the process either
way; this module only changes WHERE the bare construction lives.

Deferring through this module's `app` without ever importing
`jobcannon.host.tasks` works because `App.configure_task(name,
allow_unknown=True)` falls back to `procrastinate.tasks.configure_task(name=
name, job_manager=self.job_manager, ...)` when `name` isn't a key in
`self.tasks` (verified against procrastinate 3.9.0's source) — deferring by
a dotted-name STRING never requires the decorated function itself to be
importable in this process. `App.__init__`'s default `import_paths=None` ->
`self.import_paths = import_paths or []`, and `configure_task` unconditionally
calls `self.perform_import_paths()` -> `utils.import_all([])`, which iterates
zero import paths — a verified no-op, not a hidden import of the heavy
module. The caller MUST still pass `queue=` explicitly matching the target
task's own `@app.task(queue=...)` (e.g. `queue="maintenance"`):
`configure_task`'s fallback path defaults an unspecified queue to
procrastinate's own `DEFAULT_QUEUE` ("default"), which
`jobcannon/worker/__main__.py`'s `run_worker(queues=["scan", "maintenance"])`
never polls — a job deferred without the right `queue=` would sit forever,
never picked up, with no error anywhere.

See `jobcannon.host.tasks`'s own module docstring for the PsycopgConnector
rationale (async connector required for `App.run_worker()`) and the
`app.tasks` dotted-name-keyed registry note.

## `configure`/`ensure_open`/`close` (issues #135/#136 HIGH-1)

The web process never opened `app` at all, so a webhook's sync `.defer()`
raised `procrastinate.exceptions.AppNotOpen` before any DB I/O -- silently,
because `jobcannon.host.user_deletion.cascade_delete_user` caught it as a
generic `Exception` and only logged. Reproduced empirically against a
throwaway Postgres DB: never-open -> raised `AppNotOpen`; `with app.open()`
-> landed a real `procrastinate_jobs` row. The worker was never affected --
`jobcannon.worker.__main__.main()` opens `app` asynchronously via
`App.run_worker()` before any task runs -- so this gap was specific to the
web process's own sync defer path.

The fix is deliberately NOT "open `app` inside `wiring.init_engine_seams`":
render.yaml runs gunicorn with `--preload`, so `init_engine_seams` runs ONCE
in the master, before every worker forks. A real `psycopg_pool.ConnectionPool`
opened there would be inherited-but-broken in every forked child -- the exact
bug class `jobcannon.db.pool`'s `_reinit_after_fork` hook and this module's
sibling `wiring._reinit_posthog_after_fork` hook already exist to solve for
their own resources (#129). The activation condition that matters is
symmetric with theirs, just inverted: a pool never OPENED pre-fork cannot be
leaked into a child either, so the simplest fix is to never open one
pre-fork at all, rather than adding a third fork-rebuild hook.

`configure(database_url)` is therefore pure bookkeeping -- no I/O, safe to
call from `wiring.init_engine_seams` (which DOES run pre-fork) -- and
`ensure_open()` does the real, lazy work: it is called reactively, from
`cascade_delete_user`, only in response to an actual caught `AppNotOpen`.
That means the first (and only) real open of `app`'s own connector always
happens POST-fork, inside whichever child process handles the first webhook
delivery that actually needs it -- never in the master. It also means
`ensure_open()` is provably inert in every case that must not touch `app.
connector`:

- The worker: `run_worker()` already opened `app`'s connector asynchronously
  before any task runs, so a sweep-triggered `cascade_delete_user`'s
  `.defer()` call succeeds on the first try -- the `except AppNotOpen`
  branch, and therefore `ensure_open()`, is never reached.
- A test using `task_app.app.replace_connector(testing.InMemoryConnector())`
  (the established idiom in tests/host/test_user_deletion.py and
  test_webhooks.py): `InMemoryConnector` never raises `AppNotOpen`, so
  `.defer()` succeeds directly and `ensure_open()` is never reached --
  it can never stomp a test's deliberately-swapped connector.

`ensure_open()` rebuilds a fresh `PsycopgConnector` against the DSN
`configure()` most recently recorded (never the conninfo frozen at this
module's import time -- see `os.environ.get("DATABASE_URL", "")` below,
which only matters as a harmless placeholder now) and assigns it to both
`app.connector` and `app.job_manager.connector` -- the same two-line shape
`App.replace_connector` itself uses -- then calls the persistent, non-context
`app.open()` once. Guarded by `_open_lock` + `_opened_our_own` so concurrent
webhook deliveries in one process open at most one real connector, not one
per delivery (review-3's actual objection to a per-defer `with app.open():`).
If `configure()` was never called at all (task_app.ensure_open() reached with
no DATABASE_URL recorded -- wiring genuinely never ran in this process), it
raises `AppNotOpen` itself rather than silently doing nothing: that case is a
real wiring bug and must surface loudly, not be swallowed a second time.

`close()` is the teardown half -- only ever called by
`wiring.teardown_engine_seams`, which production code never calls (see that
function's own docstring); it exists for test isolation between throwaway
DSNs.
"""

from __future__ import annotations

import os
import threading

import procrastinate
from procrastinate import exceptions as procrastinate_exceptions

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=os.environ.get("DATABASE_URL", ""))
)

_configured_dsn: str | None = None
_open_lock = threading.Lock()
_opened_our_own = False


def configure(database_url: str | None) -> None:
    """Wiring seam (jobcannon.host.wiring.init_engine_seams/
    teardown_engine_seams). Pure bookkeeping, no I/O -- see module docstring
    for why the real open is deferred to ensure_open() instead of happening
    here."""
    global _configured_dsn
    _configured_dsn = database_url


def ensure_open() -> None:
    """Lazily, idempotently open `app`'s own connector on first need in this
    process -- see module docstring. Only ever called reactively (from
    cascade_delete_user, after catching AppNotOpen), never proactively."""
    global _opened_our_own
    with _open_lock:
        if _opened_our_own:
            return
        if not _configured_dsn:
            raise procrastinate_exceptions.AppNotOpen(
                "task_app.ensure_open: no DATABASE_URL configured -- "
                "wiring.init_engine_seams was never called in this process"
            )
        connector = procrastinate.PsycopgConnector(conninfo=_configured_dsn)
        app.connector = connector
        app.job_manager.connector = connector
        app.open()
        _opened_our_own = True


def close() -> None:
    """Test-only teardown counterpart to ensure_open() -- see module
    docstring. A no-op if ensure_open() never actually opened anything in
    this process (the common case for a process that never took the
    AppNotOpen-catch path at all)."""
    global _opened_our_own
    with _open_lock:
        if not _opened_our_own:
            return
        app.close()
        _opened_our_own = False
