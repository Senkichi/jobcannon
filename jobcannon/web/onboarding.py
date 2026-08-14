"""Picker-first onboarding: GET/POST /start (Phase 1C).

GET /start renders the picker, sourcing its title/company options from the
live corpus (jobcannon.db._feed.distinct_titles / distinct_companies —
never a hardcoded list) so the options can never drift from what the
database actually contains. Once a picker submission is pending in the
session, GET /start instead renders a "submitted, preview coming next"
confirmation state — an in-scope, independently-tested render, not a
placeholder for a later PR.

POST /start validates the submission at the boundary (an unknown seniority
level or an out-of-range years-of-experience value re-renders the form with
a 200, never a 500), then, against one pooled connection: mints an
anonymous `users` row (jobcannon.db._users.mint_anon_user) and upserts a
`profiles` row through the existing single-writer seam
(jobcannon.db._profiles.upsert_profile) — the same users-row-then-profile
ordering scripts/seed_guest_demo.py already uses for the guest_demo
sentinel, required because profiles.user_id is a FK to users(id) with no
ON CONFLICT fallback (upsert_profile raises ForeignKeyViolation against a
parent-less user_id). A repeat POST /start in the same browser session
reuses the anon id already stored in the session's `pending_picker` rather
than minting a second `users` row.

The picker collects structured selections only: target titles/companies
(corpus-derived), a small static skills-token enum (postings has no skills
column, so there is no corpus source for skill-token *options* the way
there is for titles/companies), seniority level, years of experience, and a
workplace-type preference. No free text, name, email, or resume is
collected. profiles.experience_summary and profiles.target_locations stay
NULL — workplace_type is a session-scoped filter for a later preview, never
a profile location constraint.

This route emits no events: it is a pre-signup surface and every pre-signup
surface's g.consent_granted is hardcoded False, so instrumenting a stranger
here would contradict this codebase's consent-first stance. Consent has
exactly one writer, on an authenticated surface, added in a later PR.

DAL functions are imported at MODULE level (mirroring jobcannon/web/pages.py's
documented rationale) so tests can monkeypatch
jobcannon.web.onboarding.{distinct_titles,distinct_companies,mint_anon_user,
upsert_profile,connection_factory} directly as module attributes.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from jobcannon.db._feed import distinct_companies, distinct_titles
from jobcannon.db._profiles import upsert_profile
from jobcannon.db._users import mint_anon_user
from jobcannon.db.pool import connection_factory
from jobcannon.web.anon_session import get_pending_picker, set_pending_picker

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__)

# Closed sets, validated at the request boundary — an unrecognized value
# re-renders the form instead of reaching the database. Seniority levels and
# workplace types are deliberately small/static (analogous closed sets, same
# as the skills enum below); title/company options come from the corpus.
SENIORITY_LEVELS = ("entry", "mid", "senior", "staff", "principal")
WORKPLACE_TYPES = ("any", "remote", "hybrid", "onsite")
MAX_YEARS_OF_EXPERIENCE = 60

# The form speaks lowercase ("remote"); postings.workplace_type (written by
# jobcannon/db/_jobs.py from jobcannon/engine/location_canonical.py's
# WorkplaceType Literal) holds only uppercase tokens or NULL, and
# jobcannon/db/_feed.py's _build_filters compares with exact equality — no
# lower() on either side. Storing the raw lowercase form value into
# pending_picker would make it silently match zero rows once a later PR wires
# it into that filter. "any" maps to None, which _build_filters already
# treats as "no filter applied".
_WORKPLACE_FILTERS: dict[str, str | None] = {
    "any": None,
    "remote": "REMOTE",
    "hybrid": "HYBRID",
    "onsite": "ONSITE",
}

# postings has no skills column, so — unlike titles/companies — there is no
# corpus-derived source for skill-token *options*. A small, curated,
# deliberately-static enum, framed the same way seniority levels already are
# a closed set.
SKILLS_OPTIONS = (
    "python",
    "javascript",
    "typescript",
    "sql",
    "go",
    "java",
    "react",
    "aws",
    "kubernetes",
    "product-management",
)

_EMPTY_OPTIONS: dict[str, list[str]] = {"titles": [], "companies": []}


def _corpus_options(conn: Any) -> dict[str, list[str]]:
    return {"titles": distinct_titles(conn), "companies": distinct_companies(conn)}


def _read_picker_options() -> dict[str, list[str]]:
    """Fail-closed corpus read, mirroring jobcannon/web/pages.py's
    _read_page_data shape: an unopened pool or a genuine DB outage degrades
    to empty title/company option lists (the picker still renders — its
    fixed-enum fields are unaffected) rather than a 500 on the public
    onboarding entry point."""
    try:
        with connection_factory() as conn:
            return _corpus_options(conn)
    except Exception:
        logger.warning(
            "picker option read failed (defaulting to empty corpus options)", exc_info=True
        )
        return dict(_EMPTY_OPTIONS)


def _picker_context(*, error: str | None = None) -> dict[str, Any]:
    options = _read_picker_options()
    return {
        "submitted": False,
        "error": error,
        "titles": options["titles"],
        "companies": options["companies"],
        "skills": SKILLS_OPTIONS,
        "seniority_levels": SENIORITY_LEVELS,
        "workplace_types": WORKPLACE_TYPES,
    }


def _parse_submission(form: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the raw POST body. Returns (selections, error) — exactly one
    of the two is non-None."""
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

    selections = {
        "titles": [t for t in form.getlist("titles") if t],
        "companies": [c for c in form.getlist("companies") if c],
        "skills": [s for s in form.getlist("skills") if s and s in SKILLS_OPTIONS],
        "seniority_level": seniority_level,
        "years_of_experience": years_of_experience,
        "workplace_type": _WORKPLACE_FILTERS[workplace_type],
    }
    return selections, None


@onboarding_bp.get("/start", strict_slashes=False)
def start():
    pending = get_pending_picker()
    if pending is not None:
        return render_template("onboarding_picker.html", submitted=True, pending=pending)
    return render_template("onboarding_picker.html", **_picker_context())


@onboarding_bp.post("/start", strict_slashes=False)
def start_submit():
    selections, error = _parse_submission(request.form)
    if error is not None:
        return render_template("onboarding_picker.html", **_picker_context(error=error)), 200

    pending = get_pending_picker()
    with connection_factory() as conn:
        if pending is not None and pending.get("anon_id"):
            anon_id = pending["anon_id"]
        else:
            anon_id = mint_anon_user(conn)
        upsert_profile(
            conn,
            anon_id,
            skills=selections["skills"] or None,
            target_titles=selections["titles"] or None,
            seniority_level=selections["seniority_level"],
            years_of_experience=selections["years_of_experience"],
        )

    set_pending_picker({"anon_id": anon_id, **selections})
    return redirect(url_for("onboarding.start"))
