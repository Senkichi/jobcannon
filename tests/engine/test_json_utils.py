"""Tests for json_utils module — safe_json_load, utc_now_iso, and local_today utilities.

Tests:
- utc_now_iso returns a naive ISO 8601 string (no timezone suffix)
- utc_now_iso returns a string parseable by datetime.fromisoformat
- utc_now_iso successive calls are monotonically non-decreasing
- local_today returns a string in YYYY-MM-DD format
- local_today is consistent with local_day_utc_window
- safe_json_load returns default for None input
- safe_json_load returns default for empty string input
- safe_json_load returns default for non-string input (int, list, dict)
- safe_json_load parses a valid JSON object
- safe_json_load parses a valid JSON array
- safe_json_load parses a JSON string scalar
- safe_json_load parses a JSON number scalar
- safe_json_load returns default for malformed JSON
- safe_json_load default parameter defaults to None
- safe_json_load caller-supplied default is returned on failure
"""

from datetime import datetime

import pytest

from jobcannon.engine.json_utils import (
    local_day_utc_window,
    local_today,
    safe_json_load,
    utc_now_iso,
)

# ---------------------------------------------------------------------------
# Tests: utc_now_iso
# ---------------------------------------------------------------------------


class TestUtcNowIso:
    def test_returns_string(self):
        result = utc_now_iso()
        assert isinstance(result, str)

    def test_no_timezone_suffix(self):
        result = utc_now_iso()
        # Naive ISO 8601 has no +HH:MM or Z suffix
        assert "+" not in result
        assert result.endswith("Z") is False

    def test_parseable_by_fromisoformat(self):
        result = utc_now_iso()
        parsed = datetime.fromisoformat(result)
        assert parsed.tzinfo is None  # naive datetime

    def test_format_contains_date_and_time(self):
        result = utc_now_iso()
        # Should contain 'T' separator between date and time
        assert "T" in result

    def test_successive_calls_non_decreasing(self):
        first = utc_now_iso()
        second = utc_now_iso()
        # String comparison is valid for ISO 8601 with consistent format
        assert second >= first


# ---------------------------------------------------------------------------
# Tests: local_today
# ---------------------------------------------------------------------------


class TestLocalToday:
    def test_returns_string(self):
        result = local_today()
        assert isinstance(result, str)

    def test_format_yyyy_mm_dd(self):
        result = local_today()
        # Should match YYYY-MM-DD format
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"
        # Verify it's parseable as a date
        datetime.strptime(result, "%Y-%m-%d")

    def test_consistency_with_local_day_utc_window(self):
        """local_today() should match the local date derived from local_day_utc_window()."""
        local_today_str = local_today()
        start_utc, end_utc = local_day_utc_window()
        # Convert start_utc back to local time and extract date
        start_dt = datetime.fromisoformat(start_utc)
        # The start_utc is local midnight converted to UTC, so converting back
        # to local should give us the same date
        local_midnight = start_dt.astimezone()
        local_date_from_window = local_midnight.strftime("%Y-%m-%d")
        assert local_today_str == local_date_from_window


# ---------------------------------------------------------------------------
# Tests: safe_json_load
# ---------------------------------------------------------------------------


class TestSafeJsonLoad:
    # --- None / empty / non-string inputs ---

    def test_none_returns_default_none(self):
        assert safe_json_load(None) is None

    def test_none_returns_caller_default(self):
        assert safe_json_load(None, default=[]) == []

    def test_empty_string_returns_default(self):
        assert safe_json_load("") is None

    def test_empty_string_returns_caller_default(self):
        assert safe_json_load("", default={}) == {}

    def test_non_string_int_returns_default(self):
        assert safe_json_load(42) is None  # type: ignore[arg-type]

    def test_non_string_list_returns_default(self):
        assert safe_json_load(["a", "b"]) is None  # type: ignore[arg-type]

    def test_non_string_dict_returns_default(self):
        assert safe_json_load({"key": "val"}) is None  # type: ignore[arg-type]

    def test_non_string_with_caller_default(self):
        assert safe_json_load(3.14, default="fallback") == "fallback"  # type: ignore[arg-type]

    # --- Valid JSON inputs ---

    def test_valid_json_object(self):
        result = safe_json_load('{"a": 1, "b": "two"}')
        assert result == {"a": 1, "b": "two"}

    def test_valid_json_array(self):
        result = safe_json_load('["x", "y", "z"]')
        assert result == ["x", "y", "z"]

    def test_valid_json_empty_array(self):
        result = safe_json_load("[]")
        assert result == []

    def test_valid_json_empty_object(self):
        result = safe_json_load("{}")
        assert result == {}

    def test_valid_json_string_scalar(self):
        result = safe_json_load('"hello"')
        assert result == "hello"

    def test_valid_json_integer_scalar(self):
        result = safe_json_load("42")
        assert result == 42

    def test_valid_json_float_scalar(self):
        result = safe_json_load("3.14")
        assert result == pytest.approx(3.14)

    def test_valid_json_boolean_true(self):
        result = safe_json_load("true")
        assert result is True

    def test_valid_json_boolean_false(self):
        result = safe_json_load("false")
        assert result is False

    def test_valid_json_null(self):
        # JSON null parses to Python None — this is a successful parse, not a failure
        result = safe_json_load("null", default="fallback")
        assert result is None

    def test_valid_nested_object(self):
        payload = '{"scores": [1, 2, 3], "meta": {"version": 2}}'
        result = safe_json_load(payload)
        assert result == {"scores": [1, 2, 3], "meta": {"version": 2}}

    # --- Malformed JSON inputs ---

    def test_malformed_json_returns_default(self):
        assert safe_json_load("{not valid json}") is None

    def test_malformed_json_truncated_returns_default(self):
        assert safe_json_load('{"key": ') is None

    def test_malformed_json_with_caller_default(self):
        assert safe_json_load("!@#$%", default=[]) == []

    def test_whitespace_only_returns_default(self):
        assert safe_json_load("   ") is None

    # --- Default parameter behaviour ---

    def test_default_is_none_when_not_supplied(self):
        result = safe_json_load(None)
        assert result is None

    def test_custom_default_list(self):
        result = safe_json_load(None, default=[])
        assert result == []
        assert result is not None

    def test_custom_default_dict(self):
        result = safe_json_load("", default={})
        assert result == {}

    def test_custom_default_string(self):
        result = safe_json_load("bad json", default="MISSING")
        assert result == "MISSING"

    def test_custom_default_integer(self):
        result = safe_json_load(None, default=0)
        assert result == 0
