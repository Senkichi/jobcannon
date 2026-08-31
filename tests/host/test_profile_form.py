"""jobcannon/web/profile_form.py — pure parse / prefill / echo layer for the
/profile editor (Spec 2 §2). No Flask app, no database: forms are
werkzeug MultiDicts, rows are plain dicts."""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from werkzeug.datastructures import MultiDict


def _form(**fields):
    """MultiDict from kwargs; a list value becomes repeated keys (checkbox
    groups), everything else a single value."""
    items = []
    for key, value in fields.items():
        if isinstance(value, list):
            items.extend((key, v) for v in value)
        else:
            items.append((key, value))
    return MultiDict(items)


def _valid_form(**overrides):
    fields = {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme\r\nGlobex",
        "target_locations": "Seattle, WA\n\n  Remote  \n",
        "experience_summary": "Twelve years.\r\nMostly backend.",
        "skills": ["python", "sql", "not-a-known-skill"],
        "seniority_level": "staff",
        "years_of_experience": "12.5",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }
    fields.update(overrides)
    return _form(**fields)


# --- parse_profile_form -------------------------------------------------


def test_parse_keys_are_exactly_replace_profile_kwargs():
    """Type-consistency pin between the two halves of the write path: the
    dict parse_profile_form returns is splatted straight into
    replace_profile, whose kwargs are all required. A key drift on either
    side is a TypeError in production; this catches it in a unit test."""
    from jobcannon.db._profiles import replace_profile
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form())
    assert error is None
    kwargs = {
        name
        for name, param in inspect.signature(replace_profile).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert set(parsed) == kwargs


def test_parse_valid_form_produces_a_complete_snapshot():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form())
    assert error is None
    assert parsed == {
        "skills": ["python", "sql"],  # unknown skill filtered, order kept
        "experience_summary": "Twelve years.\nMostly backend.",  # CRLF -> LF
        "target_titles": ["Staff Engineer", "Principal Engineer"],
        "target_locations": ["Seattle, WA", "Remote"],  # blanks dropped, edges stripped
        "seniority_level": "staff",
        "years_of_experience": 12.5,
        "comp_floor_usd": 180000,
        "target_companies": ["Acme", "Globex"],  # CRLF split
        "workplace_type": "REMOTE",
    }


def test_parse_empty_form_is_a_valid_all_blank_snapshot():
    """Blank everything is a legitimate submission: empty lists (a stored
    [], the deliberate-clear semantics) and NULL scalars. There is no
    'pick at least one' rule here — that rule belongs to the picker, whose
    empty submission would otherwise show an unfiltered preview."""
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_form())
    assert error is None
    assert parsed == {
        "skills": [],
        "experience_summary": None,
        "target_titles": [],
        "target_locations": [],
        "seniority_level": None,
        "years_of_experience": None,
        "comp_floor_usd": None,
        "target_companies": [],
        "workplace_type": None,
    }


def test_parse_whitespace_only_summary_is_none():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(experience_summary="  \r\n \n "))
    assert error is None
    assert parsed["experience_summary"] is None


def test_parse_blank_workplace_means_no_preference():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(workplace_type=""))
    assert error is None
    assert parsed["workplace_type"] is None


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("seniority_level", "wizard", "unrecognized seniority level"),
        ("workplace_type", "moon", "unrecognized workplace type"),
        ("years_of_experience", "abc", "years of experience must be a number"),
        ("years_of_experience", "61", "years of experience must be between 0 and 60"),
        ("years_of_experience", "-1", "years of experience must be between 0 and 60"),
        ("comp_floor_usd", "120000.50", "compensation floor must be a whole number"),
        ("comp_floor_usd", "-5", "compensation floor must be between 0 and"),
        ("comp_floor_usd", "2147483648", "compensation floor must be between 0 and"),
    ],
)
def test_parse_scalar_validation_mirrors_the_picker(field, value, fragment):
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(**{field: value}))
    assert parsed is None
    assert fragment in error


def test_parse_too_many_locations_is_rejected():
    from jobcannon.web.profile_form import MAX_LOCATIONS_PER_PROFILE, parse_profile_form

    lines = "\n".join(f"City {i}" for i in range(MAX_LOCATIONS_PER_PROFILE + 1))
    parsed, error = parse_profile_form(_valid_form(target_locations=lines))
    assert parsed is None
    assert error == f"too many locations selected (max {MAX_LOCATIONS_PER_PROFILE})"

    at_cap = "\n".join(f"City {i}" for i in range(MAX_LOCATIONS_PER_PROFILE))
    parsed, error = parse_profile_form(_valid_form(target_locations=at_cap))
    assert error is None
    assert len(parsed["target_locations"]) == MAX_LOCATIONS_PER_PROFILE


def test_parse_overlong_location_is_rejected():
    from jobcannon.web.profile_form import MAX_LOCATION_LENGTH, parse_profile_form

    parsed, error = parse_profile_form(
        _valid_form(target_locations="x" * (MAX_LOCATION_LENGTH + 1))
    )
    assert parsed is None
    assert error == f"location exceeds the {MAX_LOCATION_LENGTH}-character limit"


def test_parse_control_char_in_a_list_item_is_rejected():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(target_locations="Seattle\x00WA"))
    assert parsed is None
    assert error == "location contains invalid (control) characters"

    parsed, error = parse_profile_form(_valid_form(target_titles="Staff\x07Engineer"))
    assert parsed is None
    assert error == "title contains invalid (control) characters"


def test_parse_title_and_company_caps_reuse_the_picker_bounds():
    from jobcannon.web.onboarding import (
        MAX_COMPANIES_PER_SELECTION,
        MAX_TITLE_LENGTH,
        MAX_TITLES_PER_SELECTION,
    )
    from jobcannon.web.profile_form import parse_profile_form

    too_many_titles = "\n".join(f"Title {i}" for i in range(MAX_TITLES_PER_SELECTION + 1))
    parsed, error = parse_profile_form(_valid_form(target_titles=too_many_titles))
    assert parsed is None
    assert error == f"too many titles selected (max {MAX_TITLES_PER_SELECTION})"

    too_many_companies = "\n".join(f"Co {i}" for i in range(MAX_COMPANIES_PER_SELECTION + 1))
    parsed, error = parse_profile_form(_valid_form(target_companies=too_many_companies))
    assert parsed is None
    assert error == f"too many companies selected (max {MAX_COMPANIES_PER_SELECTION})"

    parsed, error = parse_profile_form(_valid_form(target_titles="t" * (MAX_TITLE_LENGTH + 1)))
    assert parsed is None
    assert error == f"title exceeds the {MAX_TITLE_LENGTH}-character limit"


def test_parse_summary_allows_newlines_but_no_other_control_chars():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(experience_summary="line one\n\nline three"))
    assert error is None
    assert parsed["experience_summary"] == "line one\n\nline three"

    parsed, error = parse_profile_form(_valid_form(experience_summary="tab\there"))
    assert parsed is None
    assert error == "experience summary contains invalid (control) characters"


def test_parse_summary_length_cap():
    from jobcannon.web.profile_form import MAX_EXPERIENCE_SUMMARY_LENGTH, parse_profile_form

    parsed, error = parse_profile_form(
        _valid_form(experience_summary="s" * (MAX_EXPERIENCE_SUMMARY_LENGTH + 1))
    )
    assert parsed is None
    assert error == (
        f"experience summary exceeds the {MAX_EXPERIENCE_SUMMARY_LENGTH:,}-character limit"
    )

    parsed, error = parse_profile_form(
        _valid_form(experience_summary="s" * MAX_EXPERIENCE_SUMMARY_LENGTH)
    )
    assert error is None


# --- profile_form_values / echo_form_values -----------------------------


def _row(**overrides):
    row = {
        "user_id": "user_123",
        "skills": ["python", "retired-skill"],
        "experience_summary": "Twelve years.",
        "target_titles": ["Staff Engineer", "Principal Engineer"],
        "target_locations": ["Seattle, WA"],
        "seniority_level": "staff",
        "years_of_experience": Decimal("12.5"),
        "comp_floor_usd": 180000,
        "target_companies": ["Acme"],
        "workplace_type": "REMOTE",
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_profile_form_values_maps_a_row_to_form_strings():
    from jobcannon.web.profile_form import profile_form_values

    assert profile_form_values(_row()) == {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme",
        "target_locations": "Seattle, WA",
        "experience_summary": "Twelve years.",
        "checked_skills": ["python"],  # retired option filtered out
        "seniority_level": "staff",
        "years_of_experience": "12.5",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }


def test_profile_form_values_whole_number_years_renders_without_decimal():
    from jobcannon.web.profile_form import profile_form_values

    assert (
        profile_form_values(_row(years_of_experience=Decimal("12")))["years_of_experience"] == "12"
    )


def test_profile_form_values_null_row_and_null_fields_are_blank():
    from jobcannon.web.profile_form import profile_form_values

    blank = {
        "target_titles": "",
        "target_companies": "",
        "target_locations": "",
        "experience_summary": "",
        "checked_skills": [],
        "seniority_level": "",
        "years_of_experience": "",
        "comp_floor_usd": "",
        "workplace_type": "",
    }
    assert profile_form_values(None) == blank
    assert (
        profile_form_values(
            _row(
                skills=None,
                experience_summary=None,
                target_titles=None,
                target_locations=None,
                seniority_level=None,
                years_of_experience=None,
                comp_floor_usd=None,
                target_companies=None,
                workplace_type=None,
            )
        )
        == blank
    )


def test_profile_form_values_returns_a_fresh_dict_each_call():
    """Immutability guard: mutating one caller's blank dict must not leak
    into the next caller's."""
    from jobcannon.web.profile_form import profile_form_values

    first = profile_form_values(None)
    first["checked_skills"].append("python")
    assert profile_form_values(None)["checked_skills"] == []


def test_echo_form_values_returns_raw_submission_strings():
    from jobcannon.web.profile_form import echo_form_values

    assert echo_form_values(_valid_form(years_of_experience="abc")) == {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme\r\nGlobex",
        "target_locations": "Seattle, WA\n\n  Remote  \n",
        "experience_summary": "Twelve years.\r\nMostly backend.",
        "checked_skills": ["python", "sql", "not-a-known-skill"],
        "seniority_level": "staff",
        "years_of_experience": "abc",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }
    assert echo_form_values(_form()) == {
        "target_titles": "",
        "target_companies": "",
        "target_locations": "",
        "experience_summary": "",
        "checked_skills": [],
        "seniority_level": "",
        "years_of_experience": "",
        "comp_floor_usd": "",
        "workplace_type": "",
    }


def test_workplace_form_options_derive_from_the_forward_map():
    from jobcannon.web.onboarding import _WORKPLACE_FILTERS
    from jobcannon.web.profile_form import WORKPLACE_FORM_OPTIONS

    assert WORKPLACE_FORM_OPTIONS == tuple(
        form for form, db in _WORKPLACE_FILTERS.items() if db is not None
    )
    assert "any" not in WORKPLACE_FORM_OPTIONS
