"""jobcannon.host.ingestion_tasks -- the L-0188 per-user IMAP ingest task,
its persistence helper, and the periodic enqueue tick.

No-DB shape/logic tests first (mirroring tests/host/test_scan_tasks.py and
tests/host/test_enqueue_tick.py's conventions), then Postgres-backed tests
(requires_postgres) for the consented-user query and the RLS positive
control that justifies this module's deliberate deviation from the design
note's literal `mailbox_consent AND EXISTS(active mailbox_credentials)`
predicate -- see ingestion_tasks._consented_user_ids's own docstring.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

from jobcannon.host import ingestion_tasks
from tests.host.conftest import requires_postgres

# --- task-shape registration ---


def test_run_user_ingest_registered_on_ingest_queue():
    assert ingestion_tasks.run_user_ingest.name in ingestion_tasks.app.tasks
    assert ingestion_tasks.app.tasks[ingestion_tasks.run_user_ingest.name].queue == "ingest"


def test_enqueue_imap_ingest_registered_on_ingest_queue():
    assert ingestion_tasks.enqueue_imap_ingest.name in ingestion_tasks.app.tasks
    assert ingestion_tasks.app.tasks[ingestion_tasks.enqueue_imap_ingest.name].queue == "ingest"


# --- enqueue_imap_ingest: feature-flag gate + defer/queueing-lock behavior ---


@contextlib.contextmanager
def _fake_conn_ctx():
    yield object()


def test_enqueue_imap_ingest_disabled_by_default_touches_no_db(monkeypatch):
    monkeypatch.delenv("IMAP_INGEST_ENABLED", raising=False)

    def _boom(*a, **k):
        raise AssertionError("must not query the DB when the flag is off")

    monkeypatch.setattr(ingestion_tasks, "_tick_connection", _boom)
    assert ingestion_tasks.enqueue_imap_ingest(0) == {"status": "disabled"}


def test_enqueue_imap_ingest_defers_one_task_per_consented_user_with_queueing_lock(
    monkeypatch,
):
    from procrastinate import testing

    monkeypatch.setenv("IMAP_INGEST_ENABLED", "true")
    monkeypatch.setattr(ingestion_tasks, "_consented_user_ids", lambda conn: ["user_a", "user_b"])
    monkeypatch.setattr(ingestion_tasks, "_tick_connection", _fake_conn_ctx)
    with ingestion_tasks.app.replace_connector(testing.InMemoryConnector()) as app:
        result = ingestion_tasks.enqueue_imap_ingest(0)
        jobs = list(app.connector.jobs.values())
        ingest_jobs = [j for j in jobs if j["task_name"] == ingestion_tasks.run_user_ingest.name]
        assert {j["args"]["user_id"] for j in ingest_jobs} == {"user_a", "user_b"}
        assert {j["queueing_lock"] for j in ingest_jobs} == {"ingest:user_a", "ingest:user_b"}
        assert result == {"enqueued": 2, "already_enqueued": 0}


def test_enqueue_imap_ingest_tolerates_already_enqueued(monkeypatch):
    from procrastinate import testing

    monkeypatch.setenv("IMAP_INGEST_ENABLED", "true")
    monkeypatch.setattr(ingestion_tasks, "_consented_user_ids", lambda conn: ["user_a"])
    monkeypatch.setattr(ingestion_tasks, "_tick_connection", _fake_conn_ctx)
    with ingestion_tasks.app.replace_connector(testing.InMemoryConnector()):
        ingestion_tasks.enqueue_imap_ingest(0)
        result = ingestion_tasks.enqueue_imap_ingest(0)
        assert result == {"enqueued": 0, "already_enqueued": 1}


def test_imap_ingest_enabled_env_parsing(monkeypatch):
    from jobcannon.host.config import imap_ingest_enabled

    monkeypatch.delenv("IMAP_INGEST_ENABLED", raising=False)
    assert imap_ingest_enabled() is False
    monkeypatch.setenv("IMAP_INGEST_ENABLED", "")
    assert imap_ingest_enabled() is False
    monkeypatch.setenv("IMAP_INGEST_ENABLED", "false")
    assert imap_ingest_enabled() is False
    monkeypatch.setenv("IMAP_INGEST_ENABLED", "TRUE")
    assert imap_ingest_enabled() is True


# --- _persist_jobs ---


def _fake_job(**overrides):
    from jobcannon.engine.models import Job

    kwargs = dict(
        title="Backend Engineer",
        company="Acme Corp",
        location="Remote",
        source="imap",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
    )
    kwargs.update(overrides)
    return Job(**kwargs)


def _fake_services(*, upsert_company=None, upsert_job=None):
    return SimpleNamespace(
        upsert_company=upsert_company or (lambda conn, name, **k: 1),
        upsert_job=upsert_job or (lambda conn, parsed, **k: SimpleNamespace(kind="inserted")),
    )


def test_persist_jobs_counts_each_upsert_kind(monkeypatch):
    kinds = iter(["inserted", "updated", "touched", "unchanged"])
    monkeypatch.setattr(
        ingestion_tasks,
        "get_services",
        lambda: _fake_services(
            upsert_job=lambda conn, parsed, **k: SimpleNamespace(kind=next(kinds))
        ),
    )
    jobs = [_fake_job() for _ in range(4)]
    summary = ingestion_tasks._persist_jobs(object(), jobs)
    assert summary["jobs_found"] == 4
    assert summary["jobs_new"] == 1
    assert summary["jobs_updated"] == 1
    assert summary["jobs_touched"] == 1
    assert summary["jobs_unchanged"] == 1
    assert summary["job_errors"] == []


def test_persist_jobs_drops_denylisted_company_silently(monkeypatch):
    from jobcannon.engine.parsed_job import DenylistedCompanyError

    def _raising_from_job(job, **k):
        raise DenylistedCompanyError("bad job board name")

    monkeypatch.setattr(ingestion_tasks.ParsedJob, "from_job", staticmethod(_raising_from_job))
    monkeypatch.setattr(ingestion_tasks, "get_services", lambda: _fake_services())

    summary = ingestion_tasks._persist_jobs(object(), [_fake_job()])
    assert summary["jobs_new"] == 0
    assert summary["job_errors"] == []


def test_persist_jobs_drops_listing_tile_silently(monkeypatch):
    """Parity with private's test_ingestion_funnel.py::test_listing_tile_drop_bucketed
    (funnel bucketing itself is dropped from this port, see module docstring's
    Modularity note, but the hard-drop-on-ListingTileError behavior it exercises
    is preserved and must stay covered)."""
    from jobcannon.engine.parsed_job import ListingTileError

    def _raising_from_job(job, **k):
        raise ListingTileError("result-count tile, not a real posting")

    monkeypatch.setattr(ingestion_tasks.ParsedJob, "from_job", staticmethod(_raising_from_job))
    monkeypatch.setattr(ingestion_tasks, "get_services", lambda: _fake_services())

    summary = ingestion_tasks._persist_jobs(object(), [_fake_job()])
    assert summary["jobs_new"] == 0
    assert summary["job_errors"] == []


def test_persist_jobs_passes_detected_ats_platform_and_slug_to_upsert_company(monkeypatch):
    """Regression test: private's _upsert_job_company passes BOTH
    ats_platform and ats_slug from extract_ats_from_urls(...) to
    upsert_company (job_finder/web/ingestion_runner.py @ bc30befa311,
    lines ~1284-1290). An earlier draft of this port only unpacked
    ats_platform ([0]) and silently dropped ats_slug -- caught by manual
    fidelity comparison against the private function, fixed before this
    test was added."""
    calls = []

    def _upsert_company(conn, name, **k):
        calls.append(k)
        return 1

    monkeypatch.setattr(
        ingestion_tasks, "get_services", lambda: _fake_services(upsert_company=_upsert_company)
    )
    job = _fake_job(source_url="https://boards.greenhouse.io/acme/jobs/123")
    ingestion_tasks._persist_jobs(object(), [job])

    assert len(calls) == 1
    assert calls[0]["ats_platform"] == "greenhouse"
    assert calls[0]["ats_slug"] == "acme"


def test_persist_jobs_one_bad_job_does_not_lose_the_rest(monkeypatch):
    def _upsert_company(conn, name, **k):
        if name == "Broken Co":
            raise RuntimeError("db sad")
        return 1

    monkeypatch.setattr(
        ingestion_tasks, "get_services", lambda: _fake_services(upsert_company=_upsert_company)
    )
    jobs = [_fake_job(company="Broken Co"), _fake_job(company="Good Co")]
    summary = ingestion_tasks._persist_jobs(object(), jobs)
    assert summary["jobs_new"] == 1
    assert len(summary["job_errors"]) == 1
    assert "Broken Co" in summary["job_errors"][0]


# --- run_user_ingest_task ---


def test_run_user_ingest_task_happy_path(monkeypatch):
    from jobcannon.host.ingestion.imap_intake import ImapIntakeResult

    fake_result = ImapIntakeResult(jobs=[_fake_job()], processed_uids=["1"])
    monkeypatch.setattr(
        "jobcannon.host.ingestion.imap_intake.run_imap_intake",
        lambda conn, user_id, *, resolver: fake_result,
    )
    monkeypatch.setattr(
        "jobcannon.host.credentials.build_mailbox_resolver", lambda conn, user_id: lambda: None
    )
    monkeypatch.setattr(
        ingestion_tasks,
        "get_services",
        lambda: SimpleNamespace(
            connection_factory=_fake_conn_ctx,
            vendor_account_error=None,
        ),
    )
    monkeypatch.setattr(
        ingestion_tasks,
        "_persist_jobs",
        lambda conn, jobs: {
            "jobs_new": 1,
            "jobs_updated": 0,
            "jobs_touched": 0,
            "jobs_unchanged": 0,
            "job_errors": [],
        },
    )

    result = ingestion_tasks.run_user_ingest_task("user_x")
    assert result["status"] == "ok"
    assert result["processed_uids"] == 1
    assert result["jobs_new"] == 1


def test_run_user_ingest_task_no_jobs_skips_persistence(monkeypatch):
    from jobcannon.host.ingestion.imap_intake import ImapIntakeResult

    fake_result = ImapIntakeResult(jobs=[], processed_uids=[])
    monkeypatch.setattr(
        "jobcannon.host.ingestion.imap_intake.run_imap_intake",
        lambda conn, user_id, *, resolver: fake_result,
    )
    monkeypatch.setattr(
        "jobcannon.host.credentials.build_mailbox_resolver", lambda conn, user_id: lambda: None
    )

    def _boom(conn, jobs):
        raise AssertionError("must not persist when there are no jobs")

    monkeypatch.setattr(ingestion_tasks, "_persist_jobs", _boom)
    monkeypatch.setattr(
        ingestion_tasks,
        "get_services",
        lambda: SimpleNamespace(connection_factory=_fake_conn_ctx, vendor_account_error=None),
    )

    result = ingestion_tasks.run_user_ingest_task("user_x")
    assert result == {"status": "ok", "jobs_found": 0, "processed_uids": 0}


def test_run_user_ingest_task_swallows_vendor_account_error(monkeypatch):
    class _FakeVendorAccountError(Exception):
        pass

    def _raise(*a, **k):
        raise _FakeVendorAccountError("account locked")

    monkeypatch.setattr("jobcannon.host.ingestion.imap_intake.run_imap_intake", _raise)
    monkeypatch.setattr(
        "jobcannon.host.credentials.build_mailbox_resolver", lambda conn, user_id: lambda: None
    )
    monkeypatch.setattr(
        ingestion_tasks,
        "get_services",
        lambda: SimpleNamespace(
            connection_factory=_fake_conn_ctx, vendor_account_error=_FakeVendorAccountError
        ),
    )

    result = ingestion_tasks.run_user_ingest_task("user_x")
    assert result == {"status": "vendor_account_error", "jobs_found": 0}


# --- _consented_user_ids + RLS positive control (Postgres) ---


@requires_postgres
def test_consented_user_ids_selects_only_consented_users(db_conn):
    from jobcannon.db._users import ensure_user

    a, b, c = (f"consent-{uuid.uuid4().hex[:8]}" for _ in range(3))
    ensure_user(db_conn, a, email="a@example.org")
    ensure_user(db_conn, b, email="b@example.org")
    ensure_user(db_conn, c, email="c@example.org")
    db_conn.execute("UPDATE users SET mailbox_consent = true WHERE id IN (%s, %s)", (a, b))

    ids = set(ingestion_tasks._consented_user_ids(db_conn))
    assert a in ids
    assert b in ids
    assert c not in ids


def _impersonate_nonsuperuser_reader(db_conn, *tables) -> str:
    """Same convention as tests/host/test_mailbox_credentials.py's helper of
    the same name (m0025's own RLS test file): superusers/BYPASSRLS roles
    always bypass RLS, so a query run under db_conn's own (Postgres test
    admin) role would pass even against a broken policy. Create a
    throwaway NOLOGIN role, grant it SELECT on the given tables, and SET
    ROLE to it so mailbox_credentials' tenant_isolation policy is actually
    exercised. CREATE ROLE is transactional, so db_conn's own per-test
    ROLLBACK undoes it -- callers still RESET ROLE in a finally."""
    role = f"imap_rls_test_reader_{uuid.uuid4().hex[:8]}"
    db_conn.execute(f"CREATE ROLE {role} NOLOGIN")
    for table in tables:
        db_conn.execute(f"GRANT SELECT ON {table} TO {role}")
    db_conn.execute(f"SET ROLE {role}")
    return role


@requires_postgres
def test_naive_cross_tenant_mailbox_join_returns_zero_rows_under_rls(db_conn):
    """Positive control for ingestion_tasks._consented_user_ids's documented
    deviation from design-aggregators-imap.md's literal
    `mailbox_consent AND EXISTS(active mailbox_credentials)` predicate.

    Proves the predicate is not merely "over-inclusive-but-safe" (the
    _due_company_names precedent) when applied naively against
    mailbox_credentials -- it silently returns ZERO rows for every tenant,
    including the seeded positive case, because FORCE ROW LEVEL SECURITY
    (m0025) evaluates the tenant_isolation policy's
    `current_setting('app.user_id', true) = user_id` predicate as
    always-false while no `app.user_id` session var is set. Run as a
    non-superuser role (see _impersonate_nonsuperuser_reader) -- db_conn's
    own Postgres test-admin role can create databases and would bypass RLS
    entirely, which would make this control pass even against a broken
    query.
    """
    from jobcannon.db._mailbox_credentials import set_mailbox_credential
    from jobcannon.db._users import ensure_user

    user_id = f"rls-ctrl-{uuid.uuid4().hex[:8]}"
    ensure_user(db_conn, user_id, email="rls-ctrl@example.org")
    db_conn.execute("UPDATE users SET mailbox_consent = true WHERE id = %s", (user_id,))
    set_mailbox_credential(
        db_conn,
        user_id,
        imap_host="imap.example.org",
        imap_port=993,
        auth_type="app_password",
        folder="INBOX",
        encrypted_secret=b"ciphertext",
        username_hint="r***@example.org",
    )

    join_sql = (
        "SELECT u.id FROM users u WHERE u.mailbox_consent AND EXISTS ("
        "SELECT 1 FROM mailbox_credentials mc WHERE mc.user_id = u.id AND mc.is_active)"
    )

    _impersonate_nonsuperuser_reader(db_conn, "users", "mailbox_credentials")
    try:
        # No app.user_id session var set: the control -- must return ZERO
        # rows even though a real positive case was just seeded.
        db_conn.execute("RESET app.user_id")
        no_set_config = db_conn.execute(join_sql).fetchall()
        assert no_set_config == []

        # With the owning tenant's app.user_id set, the SAME join now sees
        # its own row -- proving the query itself is correct and the empty
        # result above was RLS, not a bug in the join.
        db_conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
        with_set_config = db_conn.execute(join_sql).fetchall()
        assert [r["id"] for r in with_set_config] == [user_id]
    finally:
        db_conn.execute("RESET ROLE")
