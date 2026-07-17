import pytest

from jobcannon.engine import services


def _minimal(**overrides):
    base = dict(
        connection_factory=lambda: (_ for _ in ()).throw(NotImplementedError),
        upsert_job=lambda *a, **k: None,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda *a, **k: None,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )
    base.update(overrides)
    return services.ScanServices(**base)


def test_get_services_raises_when_unset():
    services.clear_services()
    with pytest.raises(RuntimeError):
        services.get_services()


def test_set_then_get_roundtrip():
    svc = _minimal()
    services.set_services(svc)
    assert services.get_services() is svc
    services.clear_services()


def test_services_is_frozen():
    svc = _minimal()
    with pytest.raises(Exception):
        svc.config = {}
