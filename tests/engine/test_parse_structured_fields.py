# PORTED from tests/test_parse_structured_fields.py @ 6fd9f9b31c6a32c7262de3619d247008425e2cde (private job-cannon). Ledger L-0603.
"""Tests for parse_structured_fields() — Haiku extraction of salary/location
from a fully-fetched jd_full, post-cascade.

Replaces the salary-extraction side-effect of the deleted Haiku/Sonnet
synthesis tiers (Phase 2b sub-fix RC4). Schema deliberately excludes
jd_full so the model cannot summarize the job description.

# PORT-SEAM: ``call_model`` is a required keyword-only injected parameter here
# (design note PR-4 section 1c) rather than the private module-level
# ``job_finder.web.model_provider.call_model`` import the original tests
# monkeypatched -- every unit test below passes ``call_model=`` directly
# instead of patching a module attribute.
#
# ``test_returns_empty_dict_on_no_signal`` is carried unchanged (bare ``{}``):
# an empty ``result.data`` never reaches the "always emit
# has_subcountry_constraint" block below it -- ``if not result.data or not
# result.schema_valid: return {}`` short-circuits first, both here and in the
# private original at this exact SHA.
"""

import logging
from unittest.mock import MagicMock

from jobcannon.engine._enrichment_structured_fields import (
    _STRUCTURED_FIELDS_SCHEMA,
    parse_structured_fields,
)


def test_extracts_salary_range_from_text():
    """parse_structured_fields returns a dict shaped from the model response."""
    fake_call = MagicMock(
        return_value=MagicMock(
            data={"salary_min": 150000, "salary_max": 200000, "location": "Remote US"},
            schema_valid=True,
        )
    )
    out = parse_structured_fields(
        jd_full="...The salary range is $150,000 - $200,000..." + ("x" * 200),
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    assert out == {
        "salary_min": 150000,
        "salary_max": 200000,
        "location": "Remote US",
        "has_subcountry_constraint": False,
    }


def test_does_not_emit_jd_full_field():
    """Schema MUST NOT include jd_full — the model cannot summarize the description."""
    assert "jd_full" not in _STRUCTURED_FIELDS_SCHEMA["properties"]


def test_returns_empty_dict_on_no_signal():
    """An empty result.data short-circuits on the `if not result.data` guard
    before the always-emit has_subcountry_constraint block, so the return is
    a bare {} -- not {"has_subcountry_constraint": False}."""
    fake_call = MagicMock(return_value=MagicMock(data={}, schema_valid=True))
    out = parse_structured_fields(
        jd_full="A description with no salary mentioned. " * 10,  # > 200 chars
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    assert out == {}


def test_runs_on_full_jd_not_truncated_fragments():
    """Confirm we send the full jd_full, not a truncated 2000-char prefix."""
    captured = {}

    def fake_call(**kwargs):
        # Concatenate user message contents to verify the full text reached
        msg = kwargs["messages"][0]["content"]
        captured["msg_len"] = len(msg)
        return MagicMock(data={}, schema_valid=True)

    long_jd = "Lorem ipsum " * 800  # ~9600 chars
    parse_structured_fields(
        jd_full=long_jd,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    # Allow some prompt overhead but message must include most of the JD
    assert captured["msg_len"] >= 8000


def test_parse_structured_fields_is_not_privacy_sensitive():
    """Issue #1373: parse_structured_fields opts out of privacy-consent filtering.

    The JD body, title, and company are public job-posting data, so the
    call should be dispatched with privacy_sensitive=False. This lets the
    configured cloud fallback chain be tried even when consented_providers
    is empty.
    """
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return MagicMock(data={}, schema_valid=True)

    parse_structured_fields(
        jd_full="Senior data scientist. Remote US. " * 50,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )

    assert captured["privacy_sensitive"] is False
    assert captured["tier"] == "quick"
    assert captured["purpose"] == "parse_structured_fields"


def test_parse_structured_fields_forwards_timeout_to_call_model():
    """Pins the parse_structured_fields -> call_model timeout hop (issue #1413).

    Without this, a regression that dropped `timeout=` from the call_model()
    call site inside parse_structured_fields would pass every other test in
    this suite.
    """
    captured = {}

    def fake_call(**kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return MagicMock(data={}, schema_valid=True)

    parse_structured_fields(
        jd_full="Long description. " * 50,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        timeout=17.5,
        call_model=fake_call,
    )

    assert captured["timeout"] == 17.5


# ---------------------------------------------------------------------------
# Plausibility-bound guard on the LLM path (issue #228)
# ---------------------------------------------------------------------------


def _bounded_call(data: dict) -> MagicMock:
    """Build a fake call_model return shaped like a schema-valid result."""
    return MagicMock(return_value=MagicMock(data=data, schema_valid=True))


def test_drops_implausibly_inflated_salary_min_only(caplog):
    """A single salary_min above $5M is dropped (and salary_max with it).

    The 100x-inflation case the issue cites: LLM emits salary_min=12800000
    on a $128K role. Both salary fields must be omitted from the output to
    preserve the both-or-neither semantics; location stays. Routes through
    salary_normalizer.normalize_observation with provenance="llm_extract",
    which never triggers the ats_structured-only cents-salvage rung, so the
    inflated pair still lands "implausible" exactly as the private inline
    bounds check did.
    """
    fake_call = _bounded_call(
        {"salary_min": 12_800_000, "location": "Remote US"},
    )

    with caplog.at_level(logging.WARNING, logger="jobcannon.engine._enrichment_structured_fields"):
        out = parse_structured_fields(
            jd_full="Long description. " * 50,
            job_row={"dedup_key": "anthropic|ds", "title": "DS", "company": "Anthropic"},
            conn=MagicMock(),
            config={},
            call_model=fake_call,
        )

    assert "salary_min" not in out
    assert "salary_max" not in out
    assert out.get("location") == "Remote US"
    assert any(
        "implausible salary" in rec.message and "anthropic|ds" in rec.message
        for rec in caplog.records
    ), "WARNING with job_id must be emitted on drop"


def test_drops_implausibly_inflated_ordered_pair(caplog):
    """An ordered both-inflated pair (e.g. 27.5M/37M) is fully dropped.

    Both sides land >MAX_PLAUSIBLE_ANNUAL under the "annual" hypothesis, and
    the cents-salvage rung requires provenance=="ats_structured" (this call
    is "llm_extract"), so neither side resolves -> implausible.
    """
    fake_call = _bounded_call(
        {"salary_min": 27_500_000, "salary_max": 37_000_000, "location": "SF"},
    )

    with caplog.at_level(logging.WARNING, logger="jobcannon.engine._enrichment_structured_fields"):
        out = parse_structured_fields(
            jd_full="Long description. " * 50,
            job_row={"dedup_key": "anth|safe", "title": "DS Safeguards", "company": "Anthropic"},
            conn=MagicMock(),
            config={},
            call_model=fake_call,
        )

    assert "salary_min" not in out
    assert "salary_max" not in out
    assert out.get("location") == "SF"
    assert any("implausible salary" in rec.message for rec in caplog.records)


def test_drops_salary_when_only_salary_max_out_of_bounds():
    """When salary_max is over $5M but salary_min looks sane, drop both.

    Half-open ranges would leak through the both-or-neither contract
    otherwise.
    """
    fake_call = _bounded_call(
        {"salary_min": 150_000, "salary_max": 18_000_000},
    )

    out = parse_structured_fields(
        jd_full="Long description. " * 50,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )

    assert "salary_min" not in out
    assert "salary_max" not in out


def test_drops_salary_below_minimum_plausible():
    """Sub-$30K annual salary (likely an hourly-as-annual confusion) is dropped.

    normalize_observation's salvage ladder only guesses hourly/daily/weekly/
    monthly when the period is KNOWN; the LLM path never sets salary_period,
    so period stays "unknown" and a too-low value has no salvage rung -- it
    lands implausible exactly like the private inline $30K floor check.
    """
    fake_call = _bounded_call({"salary_min": 15, "salary_max": 25})

    out = parse_structured_fields(
        jd_full="Long description. " * 50,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )

    assert "salary_min" not in out
    assert "salary_max" not in out


def test_plausible_salary_passes_through(caplog):
    """A normal $128K salary is preserved and no WARNING is emitted."""
    fake_call = _bounded_call(
        {"salary_min": 128_000, "salary_max": 160_000, "location": "Remote US"},
    )

    with caplog.at_level(logging.WARNING, logger="jobcannon.engine._enrichment_structured_fields"):
        out = parse_structured_fields(
            jd_full="Long description. " * 50,
            job_row={"dedup_key": "ok|y", "title": "T", "company": "C"},
            conn=MagicMock(),
            config={},
            call_model=fake_call,
        )

    assert out == {
        "salary_min": 128_000,
        "salary_max": 160_000,
        "location": "Remote US",
        "has_subcountry_constraint": False,
    }
    assert not any("implausible salary" in rec.message for rec in caplog.records)


def test_plausible_boundary_values_pass_through():
    """Boundary values exactly at $30K and $5M are accepted (inclusive bounds)."""
    fake_call = _bounded_call(
        {"salary_min": 30_000, "salary_max": 5_000_000},
    )

    out = parse_structured_fields(
        jd_full="Long description. " * 50,
        job_row={"dedup_key": "edge|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )

    assert out.get("salary_min") == 30_000
    assert out.get("salary_max") == 5_000_000


# ---------------------------------------------------------------------------
# #1202: residency_location + has_subcountry_constraint extraction
# ---------------------------------------------------------------------------


def test_extracts_residency_location_for_uk_based_posting():
    """Henry Schein case: JD says 'United Kingdom - Remote' / 'UK based' in
    Additional Information. The LLM extracts residency_location='United Kingdom'
    so it routes through apply_location_observation into locations_structured."""
    fake_call = MagicMock(
        return_value=MagicMock(
            data={
                "location": "Remote",
                "residency_location": "United Kingdom",
                "has_subcountry_constraint": False,
            },
            schema_valid=True,
        )
    )
    out = parse_structured_fields(
        jd_full="...United Kingdom - Remote. UK based..." + ("x" * 200),
        job_row={"dedup_key": "henry schein|senior data analyst", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    assert out.get("residency_location") == "United Kingdom"
    assert out.get("has_subcountry_constraint") is False


def test_extracts_subcountry_constraint_for_state_restricted_posting():
    """Genworth case: JD restricts remote to ~37 named Eastern/Central timezone
    states. The LLM sets has_subcountry_constraint=True (cannot be expressed as
    a single residency_location string)."""
    fake_call = MagicMock(
        return_value=MagicMock(
            data={
                "location": "Remote US",
                "has_subcountry_constraint": True,
            },
            schema_valid=True,
        )
    )
    out = parse_structured_fields(
        jd_full="...remote eligible in AL, AK, AZ, ... (37 states)..." + ("x" * 200),
        job_row={
            "dedup_key": "genworth financial|principal data analyst",
            "title": "T",
            "company": "C",
        },
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    assert out.get("has_subcountry_constraint") is True
    # residency_location should NOT be set for a sub-country constraint
    assert "residency_location" not in out


def test_extracts_residency_location_for_bangalore_onsite():
    """Fidelity case: JD states 'Location: Bangalore - EGL' with an onsite
    night shift. The LLM extracts residency_location='Bangalore, India' so it
    routes through apply_location_observation into locations_structured,
    and compute_location_fit's Row 4 fires (onsite outside candidate geography)."""
    fake_call = MagicMock(
        return_value=MagicMock(
            data={
                "location": "Bangalore",
                "residency_location": "Bangalore, India",
                "has_subcountry_constraint": False,
            },
            schema_valid=True,
        )
    )
    out = parse_structured_fields(
        jd_full="...Location: Bangalore - EGL. Night shift 11pm start..." + ("x" * 200),
        job_row={"dedup_key": "fidelity|data scientist", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    assert out.get("residency_location") == "Bangalore, India"
    assert out.get("has_subcountry_constraint") is False


def test_defaults_has_subcountry_constraint_to_false_when_omitted():
    """When the JD has no residency constraint, the LLM may omit
    has_subcountry_constraint. The parse must still emit it as False so the
    column moves from NULL to a definitive value and enrichment does not
    re-extract it.
    """
    fake_call = MagicMock(
        return_value=MagicMock(
            data={"location": "San Francisco, CA"},
            schema_valid=True,
        )
    )
    out = parse_structured_fields(
        jd_full="...Senior role in San Francisco..." + ("x" * 200),
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
        call_model=fake_call,
    )
    assert "residency_location" not in out
    assert out.get("has_subcountry_constraint") is False


def test_schema_includes_residency_fields():
    """The JSON schema must include the #1202 fields."""
    assert "residency_location" in _STRUCTURED_FIELDS_SCHEMA["properties"]
    assert "has_subcountry_constraint" in _STRUCTURED_FIELDS_SCHEMA["properties"]
