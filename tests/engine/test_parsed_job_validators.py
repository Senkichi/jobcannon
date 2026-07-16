"""
Tests for ParsedJob / UnresolvedParsedJob validators (I-07..I-13).

Coverage per acceptance criteria:
  - Each validator tested with a passing input AND a failing input.
  - An UnresolvedParsedJob multi-reason case (I-08 + I-13 in one call).
  - from_job() routing verified for each failure class.

Ported from the private repo's tests/test_parsed_job_validators.py. The only
adaptation is the denylist seam: the private repo patched
job_finder.parsed_job.load_config / get_company_denylist directly; the engine
port replaces both with parsed_job.set_denylist_provider (plan Task 1 Step
7a), so this file drives that seam instead (empty-frozenset provider = clean,
controlled-frozenset provider = denylisted).
"""

from __future__ import annotations

import pytest

from jobcannon.engine import parsed_job as parsed_job_mod
from jobcannon.engine.models import Job
from jobcannon.engine.parsed_job import (
    _TITLE_LOCATION_BLEED_RE,
    DenylistedCompanyError,
    LocationShapeError,
    ParsedJob,
    UnresolvedParsedJob,
    _has_title_cross_field_bleed,
    _is_jd_junk,
)
from jobcannon.engine.location_canonical import JobLocation

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_job(
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str = "New York, NY",
    source: str = "linkedin",
    source_url: str = "https://linkedin.com/jobs/1",
    source_id: str = "",
    description: str | None = None,
) -> Job:
    """Return a minimal valid Job instance."""
    return Job(
        title=title,
        company=company,
        location=location,
        source=source,
        source_url=source_url,
        source_id=source_id,
        description=description,
    )


def _make_location(city: str = "New York", country_code: str = "US") -> JobLocation:
    return JobLocation(
        city=city,
        region=None,
        region_code=None,
        country="United States",
        country_code=country_code,
        workplace_type="ONSITE",
        raw=city,
        unresolved=False,
    )


def _clean_patches():
    """Context manager that disables I-10 so other validators can be tested.

    The private repo patched load_config/get_company_denylist; the engine
    port drives the same "empty denylist" state through
    parsed_job.set_denylist_provider (plan Task 1 Step 7a seam).
    """
    import contextlib

    @contextlib.contextmanager
    def ctx():
        parsed_job_mod.set_denylist_provider(lambda: frozenset())
        try:
            yield
        finally:
            parsed_job_mod.set_denylist_provider(None)

    return ctx()


# ---------------------------------------------------------------------------
# I-08 — _TITLE_LOCATION_BLEED_RE (title metadata blob)
# ---------------------------------------------------------------------------


class TestI08TitleMetadataBlob:
    """Regex-level and from_job routing tests for I-08."""

    def test_regex_clean_title_no_match(self):
        """Plain titles without location bleed do not match."""
        clean_titles = [
            "Software Engineer",
            "Senior Product Manager (Remote)",
            "Engineering Manager",
            "Staff Data Scientist — Platform",
        ]
        for title in clean_titles:
            assert _TITLE_LOCATION_BLEED_RE.search(title) is None, (
                f"Title {title!r} should not match the bleed regex"
            )

    def test_regex_paren_state_code_matches(self):
        """') XX' shape (Blue State) is detected."""
        bleed_titles = [
            "Software Engineer) CA",
            "Product Manager) NY",
            "Engineering Lead) TX",
        ]
        for title in bleed_titles:
            assert _TITLE_LOCATION_BLEED_RE.search(title) is not None, (
                f"Title {title!r} should match the bleed regex"
            )

    def test_regex_paren_city_state_matches(self):
        """')City, ST' shape is detected."""
        assert _TITLE_LOCATION_BLEED_RE.search("Software Engineer) San Francisco, CA") is not None

    def test_from_job_clean_title_returns_parsed_job(self):
        """A clean title produces a ParsedJob (not UnresolvedParsedJob)."""
        job = _make_job(title="Software Engineer")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert not isinstance(result, UnresolvedParsedJob)
        assert "title_metadata_blob" not in result.unresolved_reasons

    def test_from_job_bleed_title_returns_unresolved(self):
        """A title with ') CA' shape routes to UnresolvedParsedJob."""
        job = _make_job(title="Software Engineer) CA")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert "title_metadata_blob" in result.unresolved_reasons
        assert result.raw_title == "Software Engineer) CA"

    def test_from_job_bleed_title_preserves_all_other_fields(self):
        """When I-08 fires, company, location, etc. are copied through."""
        job = _make_job(
            title="Engineer) NY",
            company="Acme Corp",
            location="New York, NY",
            description="A real job description with sufficient content.",
        )
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert result.company == "Acme Corp"
        assert result.location == "New York, NY"


# ---------------------------------------------------------------------------
# I-09 — title cross-field bleed
# ---------------------------------------------------------------------------


class TestI09TitleCrossFieldBleed:
    """Tests for the location-token-in-title cross-field validator."""

    def test_no_paren_no_bleed(self):
        """Titles without a paren-close never trigger I-09."""
        assert not _has_title_cross_field_bleed("Software Engineer", ["San Francisco, CA"])

    def test_paren_but_no_location_token_after(self):
        """Paren-close with non-location text after it does not trigger."""
        assert not _has_title_cross_field_bleed("Software Engineer (Backend)", ["Seattle, WA"])

    def test_location_token_after_paren_triggers(self):
        """Location token appearing after paren-close triggers I-09."""
        assert _has_title_cross_field_bleed(
            "Software Engineer) San Francisco", ["San Francisco, CA"]
        )

    def test_from_job_no_locations_raw_clean(self):
        """When locations_raw is empty, I-09 never fires."""
        job = _make_job(title="Software Engineer) Backend")
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"locations_raw": []})
        # I-08 doesn't fire (no state code), I-09 can't fire (no locations_raw)
        assert isinstance(result, ParsedJob)
        assert "title_cross_field_bleed" not in result.unresolved_reasons

    def test_from_job_cross_field_bleed_returns_unresolved(self):
        """Title with location token after ')' routes to UnresolvedParsedJob."""
        job = _make_job(title="Software Engineer) Seattle")
        loc = _make_location(city="Seattle")
        with _clean_patches():
            result = ParsedJob.from_job(
                job,
                source_meta={
                    "locations_raw": ["Seattle, WA"],
                    "locations_structured": [loc],
                },
            )
        assert isinstance(result, UnresolvedParsedJob)
        assert "title_cross_field_bleed" in result.unresolved_reasons


# ---------------------------------------------------------------------------
# I-10 — company denylist
# ---------------------------------------------------------------------------


class TestI10CompanyDenylist:
    """Tests for the company denylist validator."""

    def test_from_job_non_denylisted_company_passes(self):
        """A company not in the denylist produces a clean ParsedJob."""
        job = _make_job(company="Legitimate Company Inc")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)

    def test_from_job_denylisted_company_raises(self):
        """A company in the denylist raises DenylistedCompanyError.

        The denylist provider returns NORMALIZED entries (normalize_company),
        and from_job compares normalize_company(company) against them (#213).
        normalize_company('Fake Company LLC') strips 'Company' and 'LLC' as
        legal-entity tokens, yielding 'fake'.
        """
        job = _make_job(company="Fake Company LLC")
        parsed_job_mod.set_denylist_provider(lambda: frozenset({"fake"}))
        try:
            with pytest.raises(DenylistedCompanyError):
                ParsedJob.from_job(job)
        finally:
            parsed_job_mod.set_denylist_provider(None)

    def test_from_job_denylist_check_is_case_insensitive(self):
        """Denylist comparison is case-insensitive (normalize_company lowercases)."""
        job = _make_job(company="FAKE COMPANY LLC")
        parsed_job_mod.set_denylist_provider(lambda: frozenset({"fake"}))
        try:
            with pytest.raises(DenylistedCompanyError):
                ParsedJob.from_job(job)
        finally:
            parsed_job_mod.set_denylist_provider(None)

    def test_from_job_denylist_matches_suffix_variant(self):
        """#213: a suffix-free denylist entry rejects a company stored WITH a suffix."""
        job = _make_job(company="Fake Company Inc")
        parsed_job_mod.set_denylist_provider(lambda: frozenset({"fake"}))
        try:
            with pytest.raises(DenylistedCompanyError):
                ParsedJob.from_job(job)
        finally:
            parsed_job_mod.set_denylist_provider(None)


# ---------------------------------------------------------------------------
# I-07 — location shape (cross-field)
# ---------------------------------------------------------------------------


class TestI07LocationShape:
    """Tests for the locations_raw / locations_structured shape invariant."""

    def test_empty_locations_raw_is_ok(self):
        """No locations_raw means I-07 never fires."""
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"locations_raw": []})
        assert isinstance(result, ParsedJob)

    def test_both_populated_is_ok(self):
        """locations_raw + locations_structured → clean ParsedJob."""
        job = _make_job()
        loc = _make_location()
        with _clean_patches():
            result = ParsedJob.from_job(
                job,
                source_meta={
                    "locations_raw": ["New York, NY"],
                    "locations_structured": [loc],
                },
            )
        assert isinstance(result, ParsedJob)

    def test_locations_raw_without_structured_raises(self):
        """locations_raw non-empty but locations_structured empty raises."""
        job = _make_job()
        with _clean_patches():
            with pytest.raises(LocationShapeError):
                ParsedJob.from_job(
                    job,
                    source_meta={
                        "locations_raw": ["New York, NY"],
                        "locations_structured": [],  # violates I-07
                    },
                )

    def test_location_shape_error_subclass_of_value_error(self):
        """LocationShapeError is a subclass of ValueError."""
        assert issubclass(LocationShapeError, ValueError)


# ---------------------------------------------------------------------------
# I-13 — jd_full content density
# ---------------------------------------------------------------------------


class TestI13JdFullJunk:
    """Tests for the jd_full junk / content-density gate."""

    def test_good_content_passes(self):
        """A long, real-looking JD passes the gate."""
        good_jd = "x" * 250  # 250 chars, no junk prefix
        assert not _is_jd_junk(good_jd)

    def test_short_content_is_junk(self):
        """Content shorter than 200 chars is junk."""
        assert _is_jd_junk("Short content that is clearly too brief.")

    def test_sign_in_prefix_is_junk(self):
        assert _is_jd_junk("Sign in to view this job description. " + "x" * 300)

    def test_loading_prefix_is_junk(self):
        assert _is_jd_junk("Loading... please wait while we fetch this page. " + "x" * 300)

    def test_cookie_prefix_is_junk(self):
        assert _is_jd_junk("Cookie preferences: allow all cookies. " + "x" * 300)

    def test_privacy_policy_prefix_is_junk(self):
        assert _is_jd_junk("Privacy policy: we collect your data. " + "x" * 300)

    def test_404_prefix_is_junk(self):
        assert _is_jd_junk("404 — page not found. " + "x" * 300)

    def test_open_roles_prefix_is_junk(self):
        assert _is_jd_junk("Open roles at Acme Corp. " + "x" * 300)

    def test_skip_to_content_prefix_is_junk(self):
        assert _is_jd_junk("Skip to content. " + "x" * 300)

    def test_from_job_null_jd_full_skips_gate(self):
        """None jd_full is always clean (gate only fires on non-None)."""
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"jd_full": None})
        assert isinstance(result, ParsedJob)
        assert result.jd_full is None
        assert "jd_full_junk" not in result.unresolved_reasons

    def test_from_job_clean_jd_full_passes(self):
        """A real JD (≥200 chars, no junk prefix) passes."""
        real_jd = (
            "We are looking for a skilled software engineer to join our platform team. "
            "You will design and build scalable microservices, work closely with product "
            "managers, and mentor junior engineers. Requirements: 5+ years of Python experience, "
            "strong knowledge of distributed systems, experience with AWS or GCP. " * 3
        )
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"jd_full": real_jd})
        assert isinstance(result, ParsedJob)
        assert result.jd_full == real_jd
        assert "jd_full_junk" not in result.unresolved_reasons

    def test_from_job_junk_jd_full_returns_unresolved_with_jd_full_none(self):
        """Junk jd_full → UnresolvedParsedJob with jd_full cleared to None."""
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(
                job,
                source_meta={"jd_full": "Sign in to view this posting. " + "x" * 300},
            )
        assert isinstance(result, UnresolvedParsedJob)
        assert "jd_full_junk" in result.unresolved_reasons
        assert result.jd_full is None  # cleared, not discarded from row

    def test_from_job_junk_jd_full_other_fields_preserved(self):
        """When only I-13 fires, title/company/description are preserved."""
        job = _make_job(
            title="Software Engineer",
            company="Acme Corp",
            description="Short description.",
        )
        with _clean_patches():
            result = ParsedJob.from_job(
                job,
                source_meta={"jd_full": "Loading... " + "x" * 300},
            )
        assert isinstance(result, UnresolvedParsedJob)
        assert result.title == "Software Engineer"
        assert result.company == "Acme Corp"
        assert result.description == "Short description."
        assert result.jd_full is None


# ---------------------------------------------------------------------------
# Multi-reason UnresolvedParsedJob (I-08 + I-13)
# ---------------------------------------------------------------------------


class TestMultiReasonUnresolved:
    """Tests that multiple validators accumulate reasons in one UnresolvedParsedJob."""

    def test_i08_and_i13_both_fire(self):
        """A title with ') CA' AND a junk jd_full → two reasons in unresolved_reasons."""
        job = _make_job(title="Software Engineer) CA")
        junk_jd = "Sign in to apply for this role. " + "a" * 300
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"jd_full": junk_jd})
        assert isinstance(result, UnresolvedParsedJob)
        assert "title_metadata_blob" in result.unresolved_reasons
        assert "jd_full_junk" in result.unresolved_reasons
        assert len(result.unresolved_reasons) == 2
        assert result.jd_full is None

    def test_i09_and_i13_both_fire(self):
        """Cross-field location bleed AND junk jd_full accumulate together."""
        job = _make_job(title="Software Engineer) Seattle")
        loc = _make_location(city="Seattle")
        junk_jd = "Loading... " + "b" * 300
        with _clean_patches():
            result = ParsedJob.from_job(
                job,
                source_meta={
                    "locations_raw": ["Seattle, WA"],
                    "locations_structured": [loc],
                    "jd_full": junk_jd,
                },
            )
        assert isinstance(result, UnresolvedParsedJob)
        assert "title_cross_field_bleed" in result.unresolved_reasons
        assert "jd_full_junk" in result.unresolved_reasons
        assert result.jd_full is None

    def test_unresolved_reasons_is_non_empty(self):
        """UnresolvedParsedJob always carries at least one reason."""
        job = _make_job(title="Engineer) TX")  # I-08
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert len(result.unresolved_reasons) >= 1


# ---------------------------------------------------------------------------
# Import and basic construction sanity
# ---------------------------------------------------------------------------


class TestImportAndConstruction:
    """Verify ParsedJob / UnresolvedParsedJob can be imported and constructed."""

    def test_parsed_job_import(self):
        """ParsedJob and UnresolvedParsedJob are importable."""
        from jobcannon.engine.parsed_job import ParsedJob, UnresolvedParsedJob  # noqa: F401

    def test_denylisted_company_error_is_value_error(self):
        """DenylistedCompanyError is a subclass of ValueError."""
        assert issubclass(DenylistedCompanyError, ValueError)

    def test_from_job_clean_sets_dedup_key(self):
        """from_job() derives dedup_key from company + title (not caller-supplied)."""
        job = _make_job(title="Software Engineer", company="Acme Corp")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert "|" in result.dedup_key  # "acme corp|software engineer" shape
        assert "acme" in result.dedup_key.lower()

    def test_from_job_sets_sources_list(self):
        """from_job() wraps job.source in a list for ParsedJob.sources."""
        job = _make_job(source="linkedin")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert "linkedin" in result.sources

    def test_from_job_empty_source_id_becomes_none(self):
        """Job.source_id='' (the empty sentinel) is converted to None."""
        job = _make_job(source_id="")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert result.source_id is None

    def test_from_job_populated_source_id_preserved(self):
        """A non-empty source_id is preserved."""
        job = _make_job(source_id="job-abc-123")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert result.source_id == "job-abc-123"


# ---------------------------------------------------------------------------
# I-15 — salary_implausible quarantine (P1.6, D-3/D-9)
# ---------------------------------------------------------------------------


def _implausible_obs(min_value: float | None = 46.0, max_value: float | None = None) -> dict:
    """A capture-site observation dict whose salvage verdict is 'implausible'."""
    return {
        "min_value": min_value,
        "max_value": max_value,
        "period": "unknown",
        "currency": "USD",
        "provenance": "feed_string",
        "raw_text": "$46",
        "resolution": "implausible",
    }


class TestI15SalaryImplausible:
    """from_job routing for the salary_implausible quarantine reason."""

    def test_implausible_obs_with_null_pair_tags_quarantine(self):
        """Implausible observation + NULL canonical pair → salary_implausible."""
        job = _make_job()  # salary_min / salary_max default None
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"salary_observation": _implausible_obs()})
        assert isinstance(result, UnresolvedParsedJob)
        assert "salary_implausible" in result.unresolved_reasons

    def test_evidence_retained_on_the_observation_log(self):
        """The quarantined observation survives on salary_observations (D-1/D-12)."""
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"salary_observation": _implausible_obs()})
        assert len(result.salary_observations) == 1
        assert result.salary_observations[0]["resolution"] == "implausible"
        assert result.salary_min is None and result.salary_max is None

    def test_plural_observations_list_is_inspected(self):
        """An implausible entry in the plural salary_observations list also fires."""
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(
                job, source_meta={"salary_observations": [_implausible_obs()]}
            )
        assert isinstance(result, UnresolvedParsedJob)
        assert "salary_implausible" in result.unresolved_reasons

    def test_resolved_salary_does_not_tag(self):
        """A resolved ('ok') observation with a canonical pair is not quarantined."""
        job = _make_job()
        job.salary_min = 120_000
        job.salary_max = 150_000
        ok_obs = {**_implausible_obs(120_000, 150_000), "resolution": "ok"}
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"salary_observation": ok_obs})
        assert isinstance(result, ParsedJob)
        assert "salary_implausible" not in result.unresolved_reasons

    def test_no_observation_does_not_tag(self):
        """A row with no salary observation is never quarantined for salary."""
        job = _make_job()
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert "salary_implausible" not in result.unresolved_reasons

    def test_canonical_pair_present_suppresses_tag(self):
        """The gate is canonical-NULL: a present pair is never quarantined even if a
        stale observation still reads 'implausible'."""
        job = _make_job()
        job.salary_min = 130_000  # capture site DID resolve a value this sighting
        job.salary_max = 160_000
        with _clean_patches():
            result = ParsedJob.from_job(job, source_meta={"salary_observation": _implausible_obs()})
        assert isinstance(result, ParsedJob)
        assert "salary_implausible" not in result.unresolved_reasons

    def test_accumulates_with_other_reasons(self):
        """salary_implausible accumulates alongside an unrelated reason (jd_full_junk)."""
        job = _make_job()
        junk_jd = "Sign in to view this posting. " + "x" * 300
        with _clean_patches():
            result = ParsedJob.from_job(
                job,
                source_meta={
                    "salary_observation": _implausible_obs(),
                    "jd_full": junk_jd,
                },
            )
        assert isinstance(result, UnresolvedParsedJob)
        assert "salary_implausible" in result.unresolved_reasons
        assert "jd_full_junk" in result.unresolved_reasons

    def test_real_capture_path_via_salary_capture_fields(self):
        """End-to-end: a feed source's implausible value, plumbed through the real
        salary_capture_fields helper, is quarantined by from_job (capture→detect)."""
        from jobcannon.engine.salary_normalizer import SalaryObservation, salary_capture_fields

        obs = SalaryObservation(
            min_value=46.0, max_value=None, period="unknown", provenance="feed_string"
        )
        fields = salary_capture_fields(obs)  # implausible → no salary_min, obs retained
        assert "salary_min" not in fields  # capture left the canonical pair NULL
        job = Job(
            title="Data Scientist",
            company="Acme Corp",
            location="Remote",
            source="portal_jooble",
            source_url="https://example.com/ds",
            source_id="",
            **fields,
        )
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert "salary_implausible" in result.unresolved_reasons
        assert result.salary_observations[0]["resolution"] == "implausible"
