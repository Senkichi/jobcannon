"""Picker-first onboarding: GET/POST /start, GET /preview (Phase 1C).

GET /start renders the picker, sourcing its title/company options from the
live corpus (jobcannon.db._feed.distinct_titles / distinct_companies —
never a hardcoded list) so the options can never drift from what the
database actually contains. Once a picker submission is pending in the
session, GET /start instead renders a "submitted" confirmation state
linking to GET /preview — an in-scope, independently-tested render, not a
placeholder for a later PR.

POST /start validates the submission at the boundary (an unknown seniority
level, an out-of-range years-of-experience value, or a title selection that
fails shape validation — see MAX_TITLES_PER_SELECTION / MAX_TITLE_LENGTH
below — re-renders the form with a 200, never a 500), then, in one
transaction on one pooled connection: mints an
anonymous `users` row (jobcannon.db._users.mint_anon_user) and upserts a
`profiles` row through the existing single-writer seam
(jobcannon.db._profiles.upsert_profile) — the same users-row-then-profile
ordering scripts/seed_guest_demo.py already uses for the guest_demo
sentinel, required because profiles.user_id is a FK to users(id) with no
ON CONFLICT fallback (upsert_profile raises ForeignKeyViolation against a
parent-less user_id). A repeat POST /start in the same browser session
reuses the anon id already stored in the session's `pending_picker` rather
than minting a second `users` row. On success it redirects to GET /preview
(not back to /start — the picker's own "submitted" confirmation render is
still reachable by a direct repeat GET /start, e.g. a bookmarked or
back-button revisit).

The picker collects structured selections only: target titles/companies
(corpus-derived), a small static skills-token enum (postings has no skills
column, so there is no corpus source for skill-token *options* the way
there is for titles/companies), seniority level, years of experience, and a
workplace-type preference. No free text, name, email, or resume is
collected. profiles.experience_summary and profiles.target_locations stay
NULL — workplace_type is a session-scoped filter for a later preview, never
a profile location constraint.

GET /preview reads those same session-held selections and renders a ranked
list of postings driven only by them — no read of profiles.target_locations
or any other stored profile field. A visitor who never completed the
picker still gets a real page (the unfiltered live feed plus a prompt to
complete /start), never a 500. A visitor whose Clerk credentials verify as
signed in is redirected to the real feed (GET /) instead: /preview is a
pre-signup surface, and jobcannon/web/__init__.py's before_request gate
skips VERIFY_REQUEST entirely for every PUBLIC_PATHS route (it unconditionally
sets g.clerk_user = None there), so this route re-checks the verifier itself
rather than trusting an already-None g.clerk_user.

Both /start and /preview emit no events: they are pre-signup surfaces and
every pre-signup surface's g.consent_granted is hardcoded False, so
instrumenting a stranger here would contradict this codebase's consent-first
stance. Consent has exactly one writer, on an authenticated surface, added in
a later PR. The "why" chips shown per posting on /preview
(jobcannon.web.why.why_chips) are pure literal restatements of stored values
— no model call, no classification, no fit label.

DAL functions are imported at MODULE level (mirroring jobcannon/web/pages.py's
documented rationale) so tests can monkeypatch
jobcannon.web.onboarding.{distinct_titles,distinct_companies,list_feed_postings,
mint_anon_user,upsert_profile,connection_factory} directly as module
attributes.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from jobcannon.db._feed import distinct_companies, distinct_titles, list_feed_postings
from jobcannon.db._profiles import upsert_profile
from jobcannon.db._users import mint_anon_user
from jobcannon.db.pool import connection_factory
from jobcannon.web.anon_session import get_pending_picker, set_pending_picker
from jobcannon.web.why import why_chips

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__)

# Closed sets, validated at the request boundary — an unrecognized value
# re-renders the form instead of reaching the database. Seniority levels and
# workplace types are deliberately small/static (analogous closed sets, same
# as the skills enum below); title/company options come from the corpus.
SENIORITY_LEVELS = ("entry", "mid", "senior", "staff", "principal")
WORKPLACE_TYPES = ("any", "remote", "hybrid", "onsite")
MAX_YEARS_OF_EXPERIENCE = 60

# Title selections are corpus-derived but deliberately NOT membership-checked
# against the rendered option window (a legitimate title outside the current
# top-N window must remain selectable — see the module docstring). POST
# /start still accepts an arbitrary form body regardless of what the picker
# actually rendered, so nothing else bounds a submission's size before it
# reaches upsert_profile's target_titles jsonb column (issue #54).
#
# MAX_TITLES_PER_SELECTION mirrors distinct_titles()'s own `limit=50`
# corpus-option-window size (jobcannon/db/_feed.py): a real visitor manually
# checking boxes in the rendered picker can select at most that many, so a
# larger count is not a plausible human selection.
MAX_TITLES_PER_SELECTION = 50

# No real job title observed in the corpus runs anywhere near this long
# (postings.title has no DB-level length constraint — it's `text`). A fixed,
# generous bound: comfortable headroom over realistic titles while still
# closing off pasting an email address or an arbitrary text blob into a
# field meant to carry short corpus-derived job titles.
MAX_TITLE_LENGTH = 200

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


def _has_control_char(value: str) -> bool:
    """True if `value` contains a Unicode control character (category `Cc`:
    C0 controls 0x00-0x1F, DEL 0x7F, or C1 controls 0x80-0x9F). A job title
    has no legitimate use for these; rejecting them closes off embedding
    e.g. raw newlines or NUL bytes in a corpus-derived free-text field that
    reaches durable storage (issue #54)."""
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _parse_titles(form: Any) -> tuple[list[str] | None, str | None]:
    """Shape-validate the submitted title selections before they can reach
    upsert_profile's target_titles column: count cap, per-item length cap,
    type check, and a control-character rejection (issue #54's proposal).
    Deliberately NOT a membership check against the rendered option window
    — see MAX_TITLES_PER_SELECTION's module-level rationale comment."""
    raw_titles = [t for t in form.getlist("titles") if t]
    if len(raw_titles) > MAX_TITLES_PER_SELECTION:
        return None, f"too many titles selected (max {MAX_TITLES_PER_SELECTION})"

    titles: list[str] = []
    for title in raw_titles:
        if not isinstance(title, str):
            return None, "titles must be text values"
        if len(title) > MAX_TITLE_LENGTH:
            return None, f"title exceeds the {MAX_TITLE_LENGTH}-character limit"
        if _has_control_char(title):
            return None, "title contains invalid (control) characters"
        titles.append(title)
    return titles, None


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

    titles, error = _parse_titles(form)
    if error is not None:
        return None, error

    selections = {
        "titles": titles,
        # `companies` selections never reach durable storage today — there
        # is no target_companies column anywhere in this schema (upsert_profile
        # doesn't accept one); this list only flows into the session via
        # set_pending_picker for /preview's read-side filter. Left
        # presence-filtered only, matching prior behavior — a durable sink
        # would need the same cap treatment as titles above.
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
        with conn.raw.transaction():
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
    return redirect(url_for("onboarding.preview"))


def _current_identity() -> Any:
    """Re-check Clerk auth from inside a PUBLIC_PATHS route.

    jobcannon/web/__init__.py's before_request gate skips VERIFY_REQUEST
    entirely for public paths and unconditionally sets g.clerk_user = None,
    so a signed-in visitor's credentials are never evaluated by the time a
    PUBLIC_PATHS view runs. /preview needs to know anyway — a returning,
    signed-in visitor should land on the real feed, not the pre-signup
    preview — so it calls the verifier directly. Fails OPEN to "anonymous"
    on any error: the worst case is a signed-in visitor briefly sees the
    preview instead of being redirected, which is a UX miss, not the kind of
    privacy/correctness hazard a failed DB read is (contrast
    jobcannon/web/pages.py's _read_page_data, which fails CLOSED)."""
    verify = current_app.config.get("VERIFY_REQUEST")
    if verify is None:
        return None
    try:
        return verify(request)
    except Exception:
        logger.warning("preview auth re-check failed (treating as anonymous)", exc_info=True)
        return None


def _read_preview_postings(
    *, titles: list[str] | None, workplace_type: str | None, location_contains: str | None
) -> list[Any]:
    """Fail-closed corpus read, same shape as _read_picker_options: an
    unopened pool or a genuine DB outage degrades to an empty result list
    (_feed_list.html's empty-state branch still renders) rather than a 500
    on a public entry point."""
    try:
        with connection_factory() as conn:
            return list_feed_postings(
                conn,
                user_id=None,
                titles=titles,
                workplace_type=workplace_type,
                location_contains=location_contains,
            )
    except Exception:
        logger.warning("preview feed read failed (defaulting to empty result set)", exc_info=True)
        return []


def _ordering_label(rows: list[Any]) -> dict[str, Any]:
    """Honest ordering label: recency ordering is never presented as
    personalized ranking (the design constraint this function exists to
    satisfy). feed_state has no writer anywhere in this codebase yet,
    and list_feed_postings' anonymous branch hardcodes rank_score /
    ranker_version to NULL (it has no user_id to join feed_state on) — so
    for /preview specifically this is unconditionally the unranked branch
    today. Written as a real check on the rows, rather than a bare constant,
    so the same logic stays correct if a later authenticated consumer of
    _feed_list.html ever passes ranked rows through it.

    A row with rank_score set counts as "ranked" even when only some of
    `rows` qualify — deliberately: jobcannon/web/pages.py's own
    _ordering_label (the twin already wired to the real authed feed, and
    covered by tests/host/test_feed_page.py's
    test_feed_reads_rank_score_and_ranker_version_from_feed_state, which
    seeds 2 rows with only 1 ranked and still expects the version to
    render) treats a partially-ranked result set as personalized. Diverging
    here would falsify the docstring's own promise that this copy's logic
    "stays correct" once reused. What IS guarded is `ranker_version`
    itself: a mixed set of versions across the ranked rows, or a single
    consistent but falsy value (`None` or `''` — `ranker_version` is an
    unconstrained nullable `text` column, so both are representable),
    must never be presented as personalized — that combination is what let
    preview.html's unguarded `Ranked by {{ ordering.ranker_version }}.`
    render the bare "Ranked by ."."""
    ranked_versions = [r["ranker_version"] for r in rows if r["rank_score"] is not None]
    if not ranked_versions:
        return {"personalized": False, "ranker_version": None}
    version = ranked_versions[0] if len(set(ranked_versions)) == 1 else None
    if not version:
        return {"personalized": False, "ranker_version": None}
    return {"personalized": True, "ranker_version": version}


@onboarding_bp.get("/preview", strict_slashes=False)
def preview():
    if _current_identity() is not None:
        return redirect(url_for("pages.feed"))

    pending = get_pending_picker()
    selections: dict[str, Any] = pending if pending is not None else {}
    location_contains = (request.args.get("location") or "").strip() or None

    rows = _read_preview_postings(
        titles=selections.get("titles") or None,
        workplace_type=selections.get("workplace_type"),
        location_contains=location_contains,
    )
    entries = [{"row": row, "chips": why_chips(row, selections)} for row in rows]

    return render_template(
        "preview.html",
        entries=entries,
        has_selections=bool(selections),
        ordering=_ordering_label(rows),
        location_contains=location_contains or "",
    )
