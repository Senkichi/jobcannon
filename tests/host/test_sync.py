"""jobcannon/web/sync.py — `POST /sync/now`, the on-demand IMAP ingestion
trigger that satisfies L-0056's HOLD trigger (scheduler/_sync.py; see
design-aggregators-imap.md §3 and jobcannon/web/sync.py's module
docstring).

No throwaway database is needed: this route never itself touches a table,
it only defers a procrastinate task by dotted name -- same DB-free shape as
tests/host/test_account_route.py's `_app()`/`_authed_verify()` helpers,
which this file copies. Every test that lets `.defer()` actually run wraps
the call in `task_app.app.replace_connector(testing.InMemoryConnector())`
-- without it a real defer would hit whatever DATABASE_URL happens to be in
the environment, mirroring tests/host/test_user_deletion.py's own
docstring warning about the identical hazard.
"""

from __future__ import annotations

from jobcannon.web.auth import ClerkIdentity

USER_ID = "user_sync_1"


def _app(verify):
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
        }
    )


def _authed_verify(user_id=USER_ID):
    return lambda req: ClerkIdentity(user_id=user_id, claims={"sub": user_id})


def test_sync_now_is_authed_only():
    from jobcannon.web import PUBLIC_PATHS

    assert "/sync/now" not in PUBLIC_PATHS


def test_post_sync_now_401s_when_signed_out():
    app = _app(verify=lambda req: None)
    resp = app.test_client().post("/sync/now")
    assert resp.status_code == 401


def test_post_sync_now_404s_when_flag_disabled(monkeypatch):
    monkeypatch.delenv("IMAP_INGEST_ENABLED", raising=False)
    app = _app(verify=_authed_verify())
    resp = app.test_client().post("/sync/now")
    assert resp.status_code == 404


def test_post_sync_now_defers_task_with_queueing_lock_when_enabled(monkeypatch):
    from procrastinate import testing

    from jobcannon.host import task_app
    from jobcannon.web.sync import INGEST_TASK_NAME

    monkeypatch.setenv("IMAP_INGEST_ENABLED", "true")
    app = _app(verify=_authed_verify())

    with task_app.app.replace_connector(testing.InMemoryConnector()) as pa:
        resp = app.test_client().post("/sync/now")
        jobs = list(pa.connector.jobs.values())

    assert resp.status_code == 202
    assert resp.get_json() == {"status": "queued"}
    assert len(jobs) == 1
    job = jobs[0]
    assert job["task_name"] == INGEST_TASK_NAME
    assert job["queue_name"] == "ingest"
    assert job["queueing_lock"] == f"ingest:{USER_ID}"
    assert job["args"] == {"user_id": USER_ID}


def test_post_sync_now_returns_already_queued_on_repeat_call(monkeypatch):
    from procrastinate import testing

    from jobcannon.host import task_app

    monkeypatch.setenv("IMAP_INGEST_ENABLED", "true")
    app = _app(verify=_authed_verify())

    with task_app.app.replace_connector(testing.InMemoryConnector()) as pa:
        app.test_client().post("/sync/now")
        resp = app.test_client().post("/sync/now")
        jobs = list(pa.connector.jobs.values())

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "already_queued"}
    assert len(jobs) == 1


def test_ingest_task_name_matches_a_registered_worker_task():
    """L3-wiring guard, mirrors tests/host/test_user_deletion.py::
    test_purge_task_name_constant_matches_a_registered_worker_task exactly:
    proves INGEST_TASK_NAME actually matches a task
    jobcannon.host.ingestion_tasks (imported here, a test-only concession --
    jobcannon/web/sync.py itself must never import it, see its module
    docstring) has registered, so a typo can't silently defer jobs no
    worker will ever execute."""
    from jobcannon.host import ingestion_tasks
    from jobcannon.web.sync import INGEST_TASK_NAME

    assert INGEST_TASK_NAME in ingestion_tasks.app.tasks
    assert ingestion_tasks.app.tasks[INGEST_TASK_NAME].name == INGEST_TASK_NAME
    assert ingestion_tasks.app.tasks[INGEST_TASK_NAME].queue == "ingest"
