"""Tests for the shared field-alias helpers in jobcannon.engine._field_alias.

Verifies:
- extract_field first-match-wins semantics (including Lever and Greenhouse
  canonical keys).
- find_job_array handles direct list / top-level key / nested dict.
- The public constants contain the canonical platform keys at the expected
  positions so first-match-wins resolves them correctly.
"""

from __future__ import annotations

from jobcannon.engine._field_alias import (
    JOB_ARRAY_KEYS,
    JOB_TITLE_FIELDS,
    JOB_URL_FIELDS,
    extract_field,
    find_job_array,
)

# ---------------------------------------------------------------------------
# extract_field
# ---------------------------------------------------------------------------


class TestExtractField:
    def test_first_matching_key_wins(self):
        obj = {"jobTitle": "Analyst", "title": "Engineer"}
        # "title" appears before "jobTitle" in JOB_TITLE_FIELDS
        assert extract_field(obj, JOB_TITLE_FIELDS) == "Engineer"

    def test_lever_canonical_key_text(self):
        """'text' is the Lever title key — must resolve via JOB_TITLE_FIELDS."""
        obj = {"text": "Data Scientist"}
        assert extract_field(obj, JOB_TITLE_FIELDS) == "Data Scientist"

    def test_greenhouse_canonical_url_key(self):
        """'absolute_url' is the Greenhouse URL key — must resolve via JOB_URL_FIELDS."""
        obj = {"absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}
        assert extract_field(obj, JOB_URL_FIELDS) == "https://boards.greenhouse.io/acme/jobs/1"

    def test_lever_canonical_url_key(self):
        """'hostedUrl' is the Lever URL key — must resolve via JOB_URL_FIELDS."""
        obj = {"hostedUrl": "https://jobs.lever.co/acme/abc"}
        assert extract_field(obj, JOB_URL_FIELDS) == "https://jobs.lever.co/acme/abc"

    def test_fallback_alias_resolves_when_canonical_absent(self):
        obj = {"jobTitle": "Product Manager"}
        assert extract_field(obj, JOB_TITLE_FIELDS) == "Product Manager"

    def test_returns_none_when_no_key_matches(self):
        obj = {"unknown_key": "value"}
        assert extract_field(obj, JOB_TITLE_FIELDS) is None

    def test_skips_falsy_values(self):
        obj = {"title": "", "text": "Real Title"}
        assert extract_field(obj, JOB_TITLE_FIELDS) == "Real Title"

    def test_empty_dict_returns_none(self):
        assert extract_field({}, JOB_TITLE_FIELDS) is None


# ---------------------------------------------------------------------------
# find_job_array
# ---------------------------------------------------------------------------


class TestFindJobArray:
    def test_direct_list_of_dicts(self):
        data = [{"title": "A"}, {"title": "B"}]
        assert find_job_array(data) == data

    def test_top_level_jobs_key(self):
        data = {"jobs": [{"id": 1}], "total": 1}
        assert find_job_array(data) == [{"id": 1}]

    def test_top_level_results_key(self):
        data = {"results": [{"id": 2}]}
        assert find_job_array(data) == [{"id": 2}]

    def test_nested_dict(self):
        data = {"data": {"jobs": [{"a": 1}]}}
        assert find_job_array(data) == [{"a": 1}]

    def test_nested_results_items(self):
        data = {"results": {"items": [{"x": 1}]}}
        assert find_job_array(data) == [{"x": 1}]

    def test_empty_list_returns_none(self):
        assert find_job_array([]) is None

    def test_list_of_non_dicts_returns_none(self):
        assert find_job_array(["a", "b"]) is None

    def test_unrecognised_structure_returns_none(self):
        assert find_job_array({"unknown": "value"}) is None

    def test_none_input_returns_none(self):
        assert find_job_array(None) is None


# ---------------------------------------------------------------------------
# Constant ordering invariants
# ---------------------------------------------------------------------------


class TestConstantOrdering:
    def test_title_fields_starts_with_title(self):
        assert JOB_TITLE_FIELDS[0] == "title"

    def test_title_fields_contains_text_early(self):
        """'text' (Lever) must appear within the first three slots."""
        assert "text" in JOB_TITLE_FIELDS[:3]

    def test_url_fields_starts_with_url(self):
        assert JOB_URL_FIELDS[0] == "url"

    def test_url_fields_contains_hosted_url_early(self):
        """'hostedUrl' (Lever) must appear within the first three slots."""
        assert "hostedUrl" in JOB_URL_FIELDS[:3]

    def test_url_fields_contains_absolute_url_early(self):
        """'absolute_url' (Greenhouse) must appear within the first three slots."""
        assert "absolute_url" in JOB_URL_FIELDS[:3]

    def test_array_keys_contains_jobs(self):
        assert "jobs" in JOB_ARRAY_KEYS
