"""Unit tests for the PostHog fan-out wiring seam (1B Wave 2 PR 8 adversarial
review fix): _build_posthog_client and load_host_config's POSTHOG_* env
reading. No Postgres needed — these exercise pure construction/config logic,
not the pool.

Real posthog.Posthog construction is exercised directly (verified clean: it
does not open a network connection or block — it only spawns a background
consumer thread that batches on its own schedule, confirmed empirically to
add no meaningful delay even through interpreter teardown).
"""

from __future__ import annotations

from jobcannon.host.config import HostConfig
from jobcannon.host.wiring import _build_posthog_client


def test_build_posthog_client_none_without_key():
    assert _build_posthog_client(HostConfig(database_url="x")) is None


def test_build_posthog_client_constructed_with_key():
    client = _build_posthog_client(
        HostConfig(
            database_url="x", posthog_api_key="phc_test", posthog_host="https://us.i.posthog.com"
        )
    )
    assert client is not None


def test_load_host_config_reads_posthog_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_abc")
    from jobcannon.host.config import load_host_config

    cfg = load_host_config()
    assert cfg.posthog_api_key == "phc_abc"


def test_load_host_config_posthog_unset_is_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    from jobcannon.host.config import load_host_config

    cfg = load_host_config()
    assert cfg.posthog_api_key is None
    assert cfg.posthog_host is None
