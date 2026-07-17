def test_runtime_mapping_from_env(monkeypatch):
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.setenv("JC_SCAN_MEMO_TTL_S", "3600")
    monkeypatch.setenv("JC_DETAIL_FETCH_CONCURRENCY", "2")
    monkeypatch.setenv("JC_PAGE_FETCH_CONCURRENCY", "3")
    monkeypatch.setenv("JC_AUTH_BLOCK_STATUSES", "401,403,429,999")
    cfg = load_host_config()
    assert cfg.database_url == "postgresql://x@localhost/db"
    assert cfg.runtime["ats"]["scan_memo_ttl_s"] == 3600
    assert cfg.runtime["ats"]["detail_fetch_concurrency"] == 2
    assert cfg.runtime["ats"]["page_fetch_concurrency"] == 3
    assert cfg.runtime["health"]["auth_block_statuses"] == [401, 403, 429, 999]


def test_unset_knobs_are_absent_so_engine_defaults_apply(monkeypatch):
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    for var in (
        "JC_SCAN_MEMO_TTL_S",
        "JC_DETAIL_FETCH_CONCURRENCY",
        "JC_PAGE_FETCH_CONCURRENCY",
        "JC_AUTH_BLOCK_STATUSES",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_host_config()
    assert "scan_memo_ttl_s" not in cfg.runtime.get("ats", {})
    assert "auth_block_statuses" not in cfg.runtime.get("health", {})


def test_missing_database_url_raises(monkeypatch):
    import pytest

    from jobcannon.host.config import load_host_config

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        load_host_config()
