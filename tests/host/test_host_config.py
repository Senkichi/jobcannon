import pytest


def test_runtime_mapping_from_env(monkeypatch):
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.setenv("JC_SCAN_MEMO_TTL_S", "3600")
    monkeypatch.setenv("JC_DETAIL_FETCH_CONCURRENCY", "2")
    monkeypatch.setenv("JC_PAGE_FETCH_CONCURRENCY", "3")
    # Out-of-order input: a naive implementation that (re-)sorted the parsed
    # ints would still pass an in-order fixture — assert the exact, unsorted
    # sequence to actually pin ordering-preserving behavior.
    monkeypatch.setenv("JC_AUTH_BLOCK_STATUSES", "429,401,999,403")
    cfg = load_host_config()
    assert cfg.database_url == "postgresql://x@localhost/db"
    assert cfg.runtime["ats"]["scan_memo_ttl_s"] == 3600
    assert cfg.runtime["ats"]["detail_fetch_concurrency"] == 2
    assert cfg.runtime["ats"]["page_fetch_concurrency"] == 3
    assert cfg.runtime["health"]["auth_block_statuses"] == [429, 401, 999, 403]


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
    assert "detail_fetch_concurrency" not in cfg.runtime.get("ats", {})
    assert "page_fetch_concurrency" not in cfg.runtime.get("ats", {})
    assert "auth_block_statuses" not in cfg.runtime.get("health", {})


@pytest.mark.parametrize("value", ["", " "])
@pytest.mark.parametrize(
    "var",
    [
        "JC_SCAN_MEMO_TTL_S",
        "JC_DETAIL_FETCH_CONCURRENCY",
        "JC_PAGE_FETCH_CONCURRENCY",
        "JC_AUTH_BLOCK_STATUSES",
    ],
)
def test_empty_and_whitespace_knob_is_absent(monkeypatch, var, value):
    """An empty or whitespace-only value must mean absent, same as unset —
    not present-as-empty (KeyError-shaped surprise later) and not a crash."""
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    for other in (
        "JC_SCAN_MEMO_TTL_S",
        "JC_DETAIL_FETCH_CONCURRENCY",
        "JC_PAGE_FETCH_CONCURRENCY",
        "JC_AUTH_BLOCK_STATUSES",
    ):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(var, value)

    cfg = load_host_config()
    assert "scan_memo_ttl_s" not in cfg.runtime.get("ats", {})
    assert "detail_fetch_concurrency" not in cfg.runtime.get("ats", {})
    assert "page_fetch_concurrency" not in cfg.runtime.get("ats", {})
    assert "auth_block_statuses" not in cfg.runtime.get("health", {})


def test_missing_database_url_raises(monkeypatch):
    from jobcannon.host.config import load_host_config

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        load_host_config()


def test_malformed_int_knob_raises_named_error(monkeypatch):
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.setenv("JC_SCAN_MEMO_TTL_S", "abc")
    with pytest.raises(RuntimeError, match="JC_SCAN_MEMO_TTL_S"):
        load_host_config()


def test_malformed_auth_block_statuses_raises_named_error(monkeypatch):
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.setenv("JC_AUTH_BLOCK_STATUSES", "401,abc")
    with pytest.raises(RuntimeError, match="JC_AUTH_BLOCK_STATUSES"):
        load_host_config()


def test_every_declared_env_field_is_read_by_the_loader(monkeypatch):
    """Pins that HostConfig's `metadata={"env": ...}` declarations are not
    decoration: for every field that declares one, load_host_config() must
    actually read that env var into the matching attribute. Without this,
    tests/host/test_render_config.py's derived guard could go on asserting
    a declaration that governs nothing."""
    import dataclasses

    from jobcannon.host.config import HostConfig, load_host_config

    env_fields = [f for f in dataclasses.fields(HostConfig) if f.metadata.get("env")]
    assert env_fields, "HostConfig declares no env metadata — nothing to pin here"
    for f in env_fields:
        monkeypatch.setenv(f.metadata["env"], "sentinel-value")

    cfg = load_host_config()

    for f in env_fields:
        assert getattr(cfg, f.name) == "sentinel-value", f.name


@pytest.mark.parametrize("value", ["", " "])
@pytest.mark.parametrize("var", ["POSTHOG_API_KEY", "POSTHOG_HOST"])
def test_empty_and_whitespace_posthog_var_is_absent(monkeypatch, var, value):
    """Same absent-not-empty semantics as the JC_* knobs above: a whitespace-only
    POSTHOG_API_KEY/POSTHOG_HOST must not become a truthy-but-blank string."""
    from jobcannon.host.config import load_host_config

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    monkeypatch.setenv(var, value)

    cfg = load_host_config()
    assert cfg.posthog_api_key is None
    assert cfg.posthog_host is None
