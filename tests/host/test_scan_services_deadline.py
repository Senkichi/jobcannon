"""scan_deadline_s seam pins that must run WITHOUT Postgres.

Deliberately a separate module from test_scan_services_contract.py: that
file carries a module-scope ``pytestmark = requires_postgres``, under which
these two tests would silently skip in a no-Postgres environment — leaving
the production wall (wiring.py's ``scan_deadline_s=_SCAN_DEADLINE_S``)
deletable with a fully green suite. Neither test below touches a database:
one constructs the dataclass directly, the other calls build_scan_services,
which only assembles a ScanServices (it opens no pool and calls no
connection_factory).
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
