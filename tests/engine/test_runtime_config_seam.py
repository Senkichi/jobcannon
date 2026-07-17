"""Runtime-config seam: host-injectable provider replacing flask.current_app.

Covers the three private-repo readers now routed through
jobcannon.engine.runtime_config.get_runtime_config(): _auth_block_statuses,
_get_scan_memo_ttl_seconds (both in ats_platforms._registry), and
get_page_fetch_concurrency (ats_platforms._concurrency).
"""

import pytest

from jobcannon.engine import runtime_config
from jobcannon.engine.ats_platforms import _concurrency, _registry


@pytest.fixture(autouse=True)
def _restore_provider():
    """Save/restore the module-global provider (xdist safety)."""
    prior = runtime_config._provider
    yield
    runtime_config.set_config_provider(prior)


def test_no_provider_means_defaults():
    runtime_config.set_config_provider(None)
    assert _registry._auth_block_statuses() == frozenset({401, 403, 429})
    assert _registry._get_scan_memo_ttl_seconds() == 28800
    assert _concurrency.get_page_fetch_concurrency() == 4


def test_provider_values_are_read():
    runtime_config.set_config_provider(
        lambda: {
            "ats": {"scan_memo_ttl_s": 60, "page_fetch_concurrency": 2},
            "health": {"auth_block_statuses": [418]},
        }
    )
    assert _registry._auth_block_statuses() == frozenset({418})
    assert _registry._get_scan_memo_ttl_seconds() == 60
    assert _concurrency.get_page_fetch_concurrency() == 2


def test_page_fetch_concurrency_clamped_to_ceiling():
    runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 99}})
    assert _concurrency.get_page_fetch_concurrency() == 6


def test_provider_raising_runtime_error_falls_back_to_defaults():
    def _raise():
        raise RuntimeError("no app context")

    runtime_config.set_config_provider(_raise)
    assert _registry._auth_block_statuses() == frozenset({401, 403, 429})
    assert _registry._get_scan_memo_ttl_seconds() == 28800
    assert _concurrency.get_page_fetch_concurrency() == 4


def test_provider_raising_os_error_falls_back_to_defaults():
    def _raise():
        raise OSError("config file missing")

    runtime_config.set_config_provider(_raise)
    assert _registry._auth_block_statuses() == frozenset({401, 403, 429})
    assert _registry._get_scan_memo_ttl_seconds() == 28800
    assert _concurrency.get_page_fetch_concurrency() == 4


def test_provider_returning_none_means_defaults():
    runtime_config.set_config_provider(lambda: None)
    assert _registry._auth_block_statuses() == frozenset({401, 403, 429})
    assert _registry._get_scan_memo_ttl_seconds() == 28800
    assert _concurrency.get_page_fetch_concurrency() == 4
