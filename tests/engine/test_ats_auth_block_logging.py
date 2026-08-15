"""Tests for auth/anti-bot wall logging promotion.

Verifies that scanners emit WARNING logs for auth-block statuses (401/403/429)
instead of silent DEBUG, so a blocked board isn't mistaken for an empty one.

Adapted from the private repo's Flask ``app.config["JF_CONFIG"]`` /
``app.app_context()`` pattern to the engine's host-injectable
``runtime_config.set_config_provider(...)`` seam (same adaptation applied to
test_oracle_cloud_scanner.py / test_platform_scanner_registry.py /
test_smartrecruiters_scanner.py / test_workday_scanner.py). The
``reset_runtime_config_provider`` autouse fixture in tests/engine/conftest.py
clears the provider between tests, so no per-test app fixture or teardown is
needed here.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

from jobcannon.engine import runtime_config
from jobcannon.engine.ats_platforms._platforms_amazon import _fetch_postings as fetch_amazon
from jobcannon.engine.ats_platforms._registry import _auth_block_statuses, _http_get_json
from tests.engine.helpers.ats_session import ats_session_method


def test_auth_block_statuses_from_config():
    """Config-driven: the accessor reads from the runtime-config provider when registered."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [401, 403, 429]}})
    assert _auth_block_statuses() == frozenset({401, 403, 429})


def test_auth_block_statuses_default_when_no_context():
    """When no provider is registered, falls back to default {401, 403, 429}."""
    assert _auth_block_statuses() == frozenset({401, 403, 429})


def test_auth_block_statuses_custom_config():
    """Custom config override: only 429 is in the set."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [429]}})
    assert _auth_block_statuses() == frozenset({429})


def test_http_get_json_auth_block_warns(caplog):
    """_http_get_json emits WARNING for 403 (auth-block status) and returns None."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [401, 403, 429]}})
    with caplog.at_level(logging.WARNING):
        with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get_session:
            mock_get = ats_session_method(mock_get_session, "get")
            mock_resp = Mock()
            mock_resp.status_code = 403
            mock_get.return_value = mock_resp
            result = _http_get_json("http://example.com", "test_label", "test_slug")
            assert result is None
            assert any(
                "possible auth/anti-bot wall: HTTP 403" in record.message
                for record in caplog.records
            )


def test_http_get_json_502_stays_debug(caplog):
    """_http_get_json does NOT emit WARNING for 502 (not in auth-block set)."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [401, 403, 429]}})
    with caplog.at_level(logging.WARNING):
        with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get_session:
            mock_get = ats_session_method(mock_get_session, "get")
            mock_resp = Mock()
            mock_resp.status_code = 502
            mock_get.return_value = mock_resp
            result = _http_get_json("http://example.com", "test_label", "test_slug")
            assert result is None
            # No WARNING log for 502
            assert not any(
                "502" in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )


def test_scanner_403_emits_warning(caplog):
    """A scanner receiving 403 on its first fetch emits a WARNING containing the status and slug."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [401, 403, 429]}})
    with caplog.at_level(logging.WARNING):
        with patch(
            "jobcannon.engine.ats_platforms._platforms_amazon.get_session"
        ) as mock_get_session:
            mock_get = ats_session_method(mock_get_session, "get")
            mock_resp = Mock()
            mock_resp.status_code = 403
            mock_resp.json.return_value = {"jobs": []}
            mock_get.return_value = mock_resp
            result = fetch_amazon("test-slug")
            assert result == []
            assert any(
                "possible auth/anti-bot wall: HTTP 403" in record.message
                and "test-slug" in record.message
                for record in caplog.records
            )


def test_scanner_500_stays_debug(caplog):
    """A scanner receiving 500 does NOT emit a WARNING (only DEBUG)."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [401, 403, 429]}})
    with caplog.at_level(logging.WARNING):
        with patch(
            "jobcannon.engine.ats_platforms._platforms_amazon.get_session"
        ) as mock_get_session:
            mock_get = ats_session_method(mock_get_session, "get")
            mock_resp = Mock()
            mock_resp.status_code = 500
            mock_get.return_value = mock_resp
            result = fetch_amazon("test-slug")
            assert result == []
            # No WARNING log for 500
            assert not any(
                "500" in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )


def test_auth_block_statuses_config_override(caplog):
    """Custom config: 403 stays DEBUG (not in set), 429 WARNs (in set)."""
    runtime_config.set_config_provider(lambda: {"health": {"auth_block_statuses": [429]}})
    with caplog.at_level(logging.WARNING):
        with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get_session:
            mock_get = ats_session_method(mock_get_session, "get")
            # Test 403 (not in custom set)
            mock_resp = Mock()
            mock_resp.status_code = 403
            mock_get.return_value = mock_resp
            _http_get_json("http://example.com", "test_label", "test_slug")
            # No WARNING for 403
            assert not any(
                "403" in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )

            # Test 429 (in custom set)
            mock_resp.status_code = 429
            _http_get_json("http://example.com", "test_label", "test_slug")
            # WARNING for 429
            assert any(
                "possible auth/anti-bot wall: HTTP 429" in record.message
                for record in caplog.records
            )
