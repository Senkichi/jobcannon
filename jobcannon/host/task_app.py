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
"""

from __future__ import annotations

import os

import procrastinate

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=os.environ.get("DATABASE_URL", ""))
)
