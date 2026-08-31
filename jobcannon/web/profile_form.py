"""Pure parse / prefill / echo layer for the /profile editor (Spec 2 §2).

Three functions, no Flask request access, no database — `jobcannon/web/
profile.py` (the route module) is the only caller and hands in the request
form / the `get_profile` row:

- `parse_profile_form(form)` -> `(snapshot, error)`: the POST validator. The
  returned dict's keys are EXACTLY `jobcannon.db._profiles.replace_profile`'s
  keyword arguments (all required), so the route splats it straight through;
  `tests/host/test_profile_form.py` pins that key set against the writer's
  signature. Scalars mirror `jobcannon/web/onboarding.py`'s `_parse_submission`
  rules verbatim (same bounds, same messages); list fields arrive as ONE
  TEXT CONTROL EACH, one entry per line (the spec's "list input"), and are
  parsed by `_parse_lines` into the same validated list shapes the picker
  produces. Two validators are new here because no writer existed for the
  columns before: `target_locations` (count + per-item length + control
  chars, `_parse_titles`' shape) and `experience_summary` (length cap,
  control chars rejected EXCEPT newline — it is a textarea).

  Line parsing strips each entry. The picker deliberately keeps titles
  verbatim (an option's incidental edge whitespace must keep matching the
  corpus title it came from), but on a free-text surface trailing spaces are
  invisible to the visitor, so a stored title with edge whitespace is
  normalized the first time the visitor saves from here. Deliberate.

  There is NO "pick at least one title or company" rule: that belongs to the
  picker, whose empty submission would otherwise show an unfiltered preview.
  A blank editor submission is a legitimate all-clear snapshot.

- `profile_form_values(row)` -> template values from a `get_profile` row (or
  None for a user with no row yet): lists joined with "\\n", numbers as the
  strings the inputs echo (`format(years, "g")` so a whole-number numeric
  renders "12" not "12.0"), NULL -> "", skills filtered to SKILLS_OPTIONS
  (a retired option must not render an unknown checkbox), workplace via
  onboarding's `_WORKPLACE_DB_TO_FORM`.

- `echo_form_values(form)` -> the same key set straight from a rejected
  submission, so the 200 re-render shows exactly what the visitor typed
  (the `start_submit` echo contract).

Importing onboarding's underscore-prefixed helpers across `web/` modules has
precedent (`jobcannon/web/__init__.py` imports `_current_identity`); the
bounds are imported rather than copied so the two surfaces cannot drift.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from jobcannon.web.onboarding import (
    MAX_COMP_FLOOR_USD,
    MAX_COMPANIES_PER_SELECTION,
    MAX_COMPANY_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TITLES_PER_SELECTION,
    MAX_YEARS_OF_EXPERIENCE,
    SENIORITY_LEVELS,
    SKILLS_OPTIONS,
    WORKPLACE_TYPES,
    _WORKPLACE_DB_TO_FORM,
    _WORKPLACE_FILTERS,
    _has_control_char,
    _too_many_selected_message,
)

# target_locations has no picker precedent to inherit from. Ten locations is
# generous for a job search (the scoring prompt's location_fit reads the
# whole list); 80 characters covers "City, ST, Country" with room, and both
# keep the jsonb payload far inside the bounds the title/company caps were
# sized against.
MAX_LOCATIONS_PER_PROFILE = 10
MAX_LOCATION_LENGTH = 80

# experience_summary is a text column with no schema bound; 2000 characters
# (~300 words) is the scoring prompt's useful ceiling — candidate_context
# feeds the whole string in, so an unbounded field is an unbounded prompt.
MAX_EXPERIENCE_SUMMARY_LENGTH = 2000

# The select's non-blank options: every form value with a real DB value.
# Derived from the forward map, never a second hand-maintained tuple; the
# blank "No preference" option is rendered separately by the template and
# parses to None (NULL) below.
WORKPLACE_FORM_OPTIONS: tuple[str, ...] = tuple(
    form for form, db in _WORKPLACE_FILTERS.items() if db is not None
)


def _split_lines(raw: str | None) -> list[str]:
    """One entry per line: CRLF-normalize, split, strip each entry, drop
    blanks. A textarea submits CRLF per the HTML spec, so the normalization
    is load-bearing, not cosmetic."""
    text = (raw or "").replace("\r\n", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _parse_lines(
    raw: str | None, *, kind: str, singular: str, max_items: int, max_len: int
) -> tuple[list[str] | None, str | None]:
    """Shape-validate a one-per-line list field into the list shape
    upsert_profile/replace_profile store: count cap (via the picker's shared
    message helper, so the wording can't drift), per-item length cap, and
    control-character rejection — `_parse_titles`' three checks."""
    items = _split_lines(raw)
    message = _too_many_selected_message(kind, len(items), max_items)
    if message is not None:
        return None, message
    for item in items:
        if len(item) > max_len:
            return None, f"{singular} exceeds the {max_len}-character limit"
        if _has_control_char(item):
            return None, f"{singular} contains invalid (control) characters"
    return items, None


def _parse_summary(raw: str | None) -> tuple[str | None, str | None]:
    """experience_summary: CRLF-normalized, edge-stripped, blank -> None
    (NULL), length-capped, and control characters rejected EXCEPT "\\n" —
    the one control character a textarea legitimately produces. Tabs are
    rejected with the rest of category Cc: nothing downstream renders them."""
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return None, None
    if len(text) > MAX_EXPERIENCE_SUMMARY_LENGTH:
        return None, (
            f"experience summary exceeds the {MAX_EXPERIENCE_SUMMARY_LENGTH:,}-character limit"
        )
    if any(ch != "\n" and unicodedata.category(ch) == "Cc" for ch in text):
        return None, "experience summary contains invalid (control) characters"
    return text, None


def parse_profile_form(form: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a POST /profile body into a complete replace_profile snapshot.
    Returns (snapshot, error) — exactly one of the two is non-None."""
    seniority_level = form.get("seniority_level") or None
    if seniority_level is not None and seniority_level not in SENIORITY_LEVELS:
        return None, f"unrecognized seniority level: {seniority_level!r}"

    workplace_type = form.get("workplace_type") or "any"
    if workplace_type not in WORKPLACE_TYPES:
        return None, f"unrecognized workplace type: {workplace_type!r}"

    years_raw = (form.get("years_of_experience") or "").strip()
    years_of_experience: float | None = None
    if years_raw:
        try:
            years_of_experience = float(years_raw)
        except ValueError:
            return None, "years of experience must be a number"
        if not (0 <= years_of_experience <= MAX_YEARS_OF_EXPERIENCE):
            return None, f"years of experience must be between 0 and {MAX_YEARS_OF_EXPERIENCE}"

    comp_floor_raw = (form.get("comp_floor_usd") or "").strip()
    comp_floor_usd: int | None = None
    if comp_floor_raw:
        try:
            comp_floor_usd = int(comp_floor_raw)
        except ValueError:
            return None, "compensation floor must be a whole number"
        if not (0 <= comp_floor_usd <= MAX_COMP_FLOOR_USD):
            return None, f"compensation floor must be between 0 and {MAX_COMP_FLOOR_USD:,}"

    titles, error = _parse_lines(
        form.get("target_titles"),
        kind="titles",
        singular="title",
        max_items=MAX_TITLES_PER_SELECTION,
        max_len=MAX_TITLE_LENGTH,
    )
    if error is not None:
        return None, error

    companies, error = _parse_lines(
        form.get("target_companies"),
        kind="companies",
        singular="company",
        max_items=MAX_COMPANIES_PER_SELECTION,
        max_len=MAX_COMPANY_LENGTH,
    )
    if error is not None:
        return None, error

    locations, error = _parse_lines(
        form.get("target_locations"),
        kind="locations",
        singular="location",
        max_items=MAX_LOCATIONS_PER_PROFILE,
        max_len=MAX_LOCATION_LENGTH,
    )
    if error is not None:
        return None, error

    experience_summary, error = _parse_summary(form.get("experience_summary"))
    if error is not None:
        return None, error

    snapshot = {
        "skills": [s for s in form.getlist("skills") if s and s in SKILLS_OPTIONS],
        "experience_summary": experience_summary,
        "target_titles": titles,
        "target_locations": locations,
        "seniority_level": seniority_level,
        "years_of_experience": years_of_experience,
        "comp_floor_usd": comp_floor_usd,
        "target_companies": companies,
        "workplace_type": _WORKPLACE_FILTERS[workplace_type],
    }
    return snapshot, None


def _blank_form_values() -> dict[str, Any]:
    """A fresh dict every call (never a shared module constant) so a caller
    mutating its copy cannot leak into the next render."""
    return {
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


def profile_form_values(row: Any) -> dict[str, Any]:
    """get_profile row (or None: no profiles row yet) -> template values."""
    if row is None:
        return _blank_form_values()
    years = row["years_of_experience"]
    comp_floor = row["comp_floor_usd"]
    return {
        "target_titles": "\n".join(row["target_titles"] or []),
        "target_companies": "\n".join(row["target_companies"] or []),
        "target_locations": "\n".join(row["target_locations"] or []),
        "experience_summary": row["experience_summary"] or "",
        "checked_skills": [s for s in (row["skills"] or []) if s in SKILLS_OPTIONS],
        "seniority_level": row["seniority_level"] or "",
        "years_of_experience": format(years, "g") if years is not None else "",
        "comp_floor_usd": str(comp_floor) if comp_floor is not None else "",
        "workplace_type": _WORKPLACE_DB_TO_FORM.get(row["workplace_type"], ""),
    }


def echo_form_values(form: Any) -> dict[str, Any]:
    """Rejected submission -> template values, verbatim. Skills are echoed
    unfiltered so an unknown value re-renders nothing (the template only
    iterates SKILLS_OPTIONS) rather than being silently dropped from the
    echo — what the visitor checked is what stays checked."""
    return {
        "target_titles": form.get("target_titles") or "",
        "target_companies": form.get("target_companies") or "",
        "target_locations": form.get("target_locations") or "",
        "experience_summary": form.get("experience_summary") or "",
        "checked_skills": list(form.getlist("skills")),
        "seniority_level": form.get("seniority_level") or "",
        "years_of_experience": form.get("years_of_experience") or "",
        "comp_floor_usd": form.get("comp_floor_usd") or "",
        "workplace_type": form.get("workplace_type") or "",
    }
