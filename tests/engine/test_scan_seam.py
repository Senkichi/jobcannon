"""End-to-end seam check: a fake host wires ScanServices, a stubbed scanner
returns one posting, and the upsert path is exercised without any Flask/db
machinery. This is the contract test 1B's real host must also pass."""

import contextlib
import sqlite3

from jobcannon.engine import services
from jobcannon.engine.ats_scanner import _run


class _FakeUpsertResult:
    """Mirrors job_finder.db._jobs.UpsertResult — the real call sites
    dereference .kind and .dedup_key on the return value, so a None-returning
    fake crashes them (plan-review finding F3)."""

    def __init__(self, kind="inserted", dedup_key="fake-dedup-key", unresolved_reasons=None):
        self.kind = kind
        self.dedup_key = dedup_key
        self.unresolved_reasons = unresolved_reasons or []


def _fake_services(upserts):
    @contextlib.contextmanager
    def factory(*, synchronous="FULL"):
        con = sqlite3.connect(":memory:")
        try:
            yield con
        finally:
            con.close()

    def fake_upsert_job(conn, parsed, **kw):
        upserts.append((parsed, kw))
        return _FakeUpsertResult()

    return services.ScanServices(
        connection_factory=factory,
        upsert_job=fake_upsert_job,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda conn, name, *a, **k: 1,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )


def test_upsert_flows_through_injected_persistence():
    """Feed _upsert_one_ats_api_job a minimal ATS-scanner job_dict (same shape
    the private repo's test_ats_scanner_run.py::_job_dict builds — title +
    company_source are the only keys _upsert_one_ats_api_job dereferences
    unconditionally, everything else falls back via .get()) and assert the
    injected upsert_job hook is actually invoked exactly once, with no
    Flask/db machinery anywhere in the path."""
    upserts = []
    services.set_services(_fake_services(upserts))
    try:
        summary: dict = {"jobs_new": 0, "errors": []}
        all_new_job_keys: list = []
        job_dict = {"title": "Staff Data Engineer", "company_source": "Ashby"}

        with sqlite3.connect(":memory:") as conn_outer, sqlite3.connect(":memory:") as scan_conn:
            _run._upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                job_dict,
                summary,
                all_new_job_keys,
                company_id=1,
                ats_platform="ashby",
            )

        assert summary["errors"] == []
        assert len(upserts) == 1
        parsed, kw = upserts[0]
        assert parsed.title == "Staff Data Engineer"
        assert parsed.company == "AshbyCo"
        assert kw == {"company_id": 1, "ats_platform": "ashby"}
        assert summary["jobs_new"] == 1
        assert all_new_job_keys == ["fake-dedup-key"]
    finally:
        services.clear_services()


def test_optional_hooks_default_to_skip():
    svc_list = []
    services.set_services(_fake_services(svc_list))
    try:
        svc = services.get_services()
        assert svc.score_and_persist_job is None
        assert svc.enrich_job is None
        assert svc.run_heal_pass is None
        assert svc.find_careers_url is None
        assert svc.scrape_careers_page is None
        assert svc.run_homepage_discovery is None
        assert svc.run_detection is None
        assert svc.identity_reconcile_settings is None
        assert svc.promote_ats_scheduler_batch is None
        assert svc.reconcile_company_ats is None
        assert svc.owner_identity_passes is None
        assert svc.resolve_slug_collision is None
        assert svc.prober_extensions is None
    finally:
        services.clear_services()
