# PORTED from job_finder/web/_field_alias.py @ 7491ce38a9c8abe10973e5c443ed113f5996ad42 (private job-cannon). Ledger L-0449.
"""Shared field-alias helpers for JSON job-posting extraction.

Provides the canonical key lists and first-match-wins extractors shared
across the ATS platform scanners (Greenhouse, Lever, …). In the private
source these were also shared with a generic careers-page AI navigator
(``careers_page_interactions.py``) that was not carried into this port.
# PORT-SEAM: docstring reworded for the engine boundary; see the
# override-loader seam below for the other private-source divergence.

**Key ordering matters — first-match-wins.**
Each platform's real key must appear *before* any alias:

- ``JOB_TITLE_FIELDS``: ``title`` (Greenhouse), ``text`` (Lever), then
  broader aliases.
- ``JOB_URL_FIELDS``: ``url`` generic, then ``hostedUrl`` (Lever),
  ``absolute_url`` (Greenhouse), then broader aliases.

Adding a new key that belongs between existing ones? Insert it in the
correct position — do not append to the end.
"""

from __future__ import annotations

# PORT-SEAM: host-injectable override-loader seam (was job_finder.web.autoheal.override_loader).
# None => no overrides => canonical field-alias behavior (byte-identical per the
# module contract). Hosts inject an object with the same interface the private
# repo's autoheal.override_loader module exposes at the two call sites below.
_override_loader = None


def set_override_loader(loader) -> None:
    global _override_loader
    _override_loader = loader


# ---------------------------------------------------------------------------
# Canonical key lists (shared across surfaces)
# ---------------------------------------------------------------------------

JOB_ARRAY_KEYS: list[str] = [
    "jobs",
    "results",
    "data",
    "positions",
    "openings",
    "postings",
    "items",
    "jobPostings",
    "records",
    "hits",
]

# Title key priority:
#   title        — Greenhouse, generic
#   text         — Lever
#   name         — generic fallback
#   jobTitle     — schema.org / some custom platforms
#   job_title    — underscore variant
#   positionTitle — some legacy APIs
#   role         — informal alias
#   position     — generic fallback
JOB_TITLE_FIELDS: list[str] = [
    "title",
    "text",
    "name",
    "jobTitle",
    "job_title",
    "positionTitle",
    "role",
    "position",
]

# URL key priority:
#   url           — generic
#   hostedUrl     — Lever
#   absolute_url  — Greenhouse
#   applyUrl      — schema.org / some custom platforms
#   apply_url     — underscore variant
#   link / href   — generic HTML-derived
#   detailUrl / detail_url / jobUrl / canonicalUrl — misc aliases
JOB_URL_FIELDS: list[str] = [
    "url",
    "hostedUrl",
    "absolute_url",
    "applyUrl",
    "apply_url",
    "link",
    "href",
    "detailUrl",
    "detail_url",
    "jobUrl",
    "canonicalUrl",
]


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_field(obj: dict, field_names: list[str]):
    """Return the first non-falsy value from *obj* keyed by *field_names*.

    Returns ``None`` when none of the keys are present or all mapped values
    are falsy.  Callers that need a guaranteed ``str`` should coalesce:
    ``extract_field(obj, JOB_TITLE_FIELDS) or ""``.
    """
    for name in field_names:
        if obj.get(name):
            return obj[name]
    return None


def find_job_array(data) -> list | None:
    """Return the array of job-posting dicts from a parsed JSON response.

    Handles three shapes:
    - Direct list: ``[{...}, ...]``
    - Top-level keyed: ``{"jobs": [...]}``
    - One-level nested: ``{"data": {"jobs": [...]}}``

    Returns ``None`` when no recognisable job array is found.
    """
    if isinstance(data, list):
        return data if data and isinstance(data[0], dict) else None

    if isinstance(data, dict):
        for key in JOB_ARRAY_KEYS:
            if key in data and isinstance(data[key], list):
                return data[key]

        # Check nested: {data: {jobs: [...]}} or {results: {items: [...]}}
        for outer_key in ("data", "results", "response", "body"):
            if outer_key in data and isinstance(data[outer_key], dict):
                inner = data[outer_key]
                for key in JOB_ARRAY_KEYS:
                    if key in inner and isinstance(inner[key], list):
                        return inner[key]

    return None


# PORT-SEAM: resolve_title/resolve_url/resolve_job_array below are an
# engine-side addition from an earlier port wave, wrapping the canonical
# extractors with the override-loader seam above; no host-layer dependency.
# ---------------------------------------------------------------------------
# Override-aware resolvers (Phase C / C2 — dormant without an override file)
# ---------------------------------------------------------------------------
#
# Each resolver consults the autoheal override loader for `ats:{platform}`.
# Override extras are appended AFTER the canonical list so first-match-wins
# on un-renamed data is preserved. With no override file present these are
# byte-identical to the canonical extract_field / find_job_array calls.


def _with_extras(canonical: list[str], platform: str, attr: str) -> list[str]:
    """Return canonical key list extended with override extras (canonical first)."""
    if _override_loader is None:
        return canonical
    recipe = _override_loader.ats_alias(f"ats:{platform}")
    if recipe is None:
        return canonical
    extras = [k for k in getattr(recipe, attr) if k not in canonical]
    return canonical + extras if extras else canonical


def resolve_title(posting: dict, platform: str):
    """Override-aware job-title resolution for an ATS *platform* posting."""
    return extract_field(posting, _with_extras(JOB_TITLE_FIELDS, platform, "title_fields"))


def resolve_url(posting: dict, platform: str):
    """Override-aware job-URL resolution for an ATS *platform* posting."""
    return extract_field(posting, _with_extras(JOB_URL_FIELDS, platform, "url_fields"))


def resolve_job_array(data, platform: str) -> list | None:
    """Override-aware job-array location: canonical find_job_array first, then extras."""
    found = find_job_array(data)
    if found is not None:
        return found
    if _override_loader is None:
        return None
    recipe = _override_loader.ats_alias(f"ats:{platform}")
    if recipe is None or not recipe.array_keys:
        return None
    if not isinstance(data, dict):
        return None
    for key in recipe.array_keys:
        if key in data and isinstance(data[key], list):
            return data[key]
    for outer_key in ("data", "results", "response", "body"):
        inner = data.get(outer_key)
        if isinstance(inner, dict):
            for key in recipe.array_keys:
                if key in inner and isinstance(inner[key], list):
                    return inner[key]
    return None
