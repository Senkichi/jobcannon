"""Task-shape declaration tests — no DB, no worker, no procrastinate schema.

Adapted from the PR draft: procrastinate 3.9.0 (pinned in pyproject.toml)
keys `app.tasks` by each task's FULLY QUALIFIED dotted name
(``jobcannon.host.tasks.scan``), not the bare function name — verified
empirically against the installed version. Tests below assert membership
via each task object's own `.name` attribute rather than hardcoding the
dotted path, so they stay correct if the module is ever renamed.
"""

import pytest

from jobcannon.host import tasks


def test_taxonomy_task_names():
    assert tasks.scan.name in tasks.app.tasks
    assert tasks.expiry_check.name in tasks.app.tasks
    assert tasks.stale_detect.name in tasks.app.tasks


def test_enrich_task_is_not_defined():
    assert not hasattr(tasks, "enrich")
    bare_names = {name.rsplit(".", 1)[-1] for name in tasks.app.tasks}
    assert "enrich" not in bare_names


def test_expiry_and_stale_are_reserved_stubs():
    from jobcannon.host import scan_tasks

    for fn in (scan_tasks.run_expiry_check_task, scan_tasks.run_stale_detect_task):
        with pytest.raises(NotImplementedError):
            fn()
