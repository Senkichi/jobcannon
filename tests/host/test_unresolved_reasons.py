"""append_reason / remove_reasons — single point of enforcement for mutating
``postings.unresolved_reasons``.

Pure unit tests (no DB): unlike the private original's TEXT/JSON-string
column, ``postings.unresolved_reasons`` is Postgres ``jsonb`` and psycopg
round-trips it as a native Python value, so these helpers take/return
``list[str]`` directly.
"""

from jobcannon.db._unresolved_reasons import append_reason, remove_reasons


class TestAppendReason:
    def test_appends_to_none(self):
        assert append_reason(None, "location_missing") == ["location_missing"]

    def test_appends_to_empty_list(self):
        assert append_reason([], "location_missing") == ["location_missing"]

    def test_preserves_existing_unrelated_reasons(self):
        result = append_reason(["location_missing"], "jd_full_offsite")
        assert result == ["location_missing", "jd_full_offsite"]

    def test_deduped_when_already_present(self):
        result = append_reason(["jd_full_offsite"], "jd_full_offsite")
        assert result == ["jd_full_offsite"]

    def test_tolerates_non_list_value(self):
        assert append_reason({"not": "a list"}, "location_missing") == ["location_missing"]


class TestRemoveReasons:
    """Mirrors append_reason's tested corruption-tolerance contract.

    remove_reasons shares append_reason's tolerant-of-non-list-value contract
    because the column carries no shape guarantee beyond NOT NULL DEFAULT
    '[]'. A corrupt row must never raise — it falls back to "no prior
    reasons" rather than propagating the corruption.
    """

    def test_removes_from_none(self):
        assert remove_reasons(None, ["jd_full_truncated"]) == []

    def test_removes_from_empty_list(self):
        assert remove_reasons([], ["jd_full_truncated"]) == []

    def test_removes_present_reason(self):
        result = remove_reasons(["jd_full_truncated"], ["jd_full_truncated"])
        assert result == []

    def test_preserves_existing_unrelated_reasons(self):
        result = remove_reasons(["location_missing", "jd_full_truncated"], ["jd_full_truncated"])
        assert result == ["location_missing"]

    def test_noop_when_reason_not_present(self):
        result = remove_reasons(["location_missing"], ["jd_full_truncated"])
        assert result == ["location_missing"]

    def test_removes_all_listed_codes(self):
        result = remove_reasons(
            ["jd_full_offsite", "jd_full_truncated", "location_missing", "jd_full_expired"],
            ["jd_full_offsite", "jd_full_truncated", "jd_full_expired"],
        )
        assert result == ["location_missing"]

    def test_tolerates_non_list_value(self):
        assert remove_reasons({"not": "a list"}, ["jd_full_truncated"]) == []

    def test_empty_remove_list_is_noop(self):
        result = remove_reasons(["location_missing"], [])
        assert result == ["location_missing"]
