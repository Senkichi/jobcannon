"""scan_deadline_s seam pins that must run WITHOUT Postgres.

Deliberately a separate module from test_scan_services_contract.py: that
file carries a module-scope ``pytestmark = requires_postgres``, under which
these tests would silently skip in a no-Postgres environment — leaving the
production wall (wiring.py's ``scan_deadline_s=_SCAN_DEADLINE_S``)
deletable with a fully green suite. No test below touches a database: they
construct the dataclass directly, call build_scan_services (which only
assembles a ScanServices — no pool, no connection_factory call), or read
the env-backed config loader.
"""


def test_scan_deadline_s_omitted_defaults_to_none():
    """ScanServices must stay constructible without scan_deadline_s (existing
    construction sites pass no such kwarg), and the field must default to
    None (unbounded)."""
    from jobcannon.engine import services

    svc = services.ScanServices(
        connection_factory=lambda **kw: None,
        upsert_job=lambda *a, **kw: None,
        set_jd_full=lambda *a, **kw: None,
        upsert_company=lambda *a, **kw: None,
        config={},
        get_secret=lambda name, *, config=None: None,
        jd_storage_max_chars=50_000,
    )
    assert svc.scan_deadline_s is None


def test_build_scan_services_sets_scan_deadline_s():
    """Host wiring must supply a positive finite wall — deleting the
    ``scan_deadline_s=`` line in build_scan_services turns this red in any
    environment, Postgres or not."""
    from jobcannon.host.config import HostConfig
    from jobcannon.host.wiring import build_scan_services

    svc = build_scan_services(HostConfig(database_url="postgresql://unused"))
    assert isinstance(svc.scan_deadline_s, float)
    assert svc.scan_deadline_s > 0


def test_hosted_config_stays_on_the_serial_scan_branch(monkeypatch):
    """Tripwire for the issue #39 deadlock precondition.

    The engine's concurrent scan branch (``scan_concurrency > 1``) deadlocks
    when the scan deadline trips with submitted work still queued (issue
    #39). Hosted is safe only because the host config loader passes no
    ``scan_concurrency`` knob through, so the engine resolves its default of
    1 and stays on the serial branch. Asserted two ways, because the
    loader's optional-knob semantics (unset -> absent from the mapping)
    would let a pass-through hide from a purely behavioral check whenever
    the env var happens to be unset: the loader's source must not mention
    the knob outside comments, and the mapping it builds must resolve to
    concurrency 1. If this fails because a pass-through was added: fix
    issue #39 first, then update this test alongside it.
    """
    import inspect

    from jobcannon.engine.ats_platforms import _concurrency
    from jobcannon.host import config as host_config

    code_lines = [line.split("#", 1)[0] for line in inspect.getsource(host_config).splitlines()]
    assert not any("scan_concurrency" in line for line in code_lines)

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    runtime = host_config.load_host_config().runtime
    assert _concurrency.get_scan_concurrency(runtime) == 1
