"""Picker-first onboarding: GET/POST /start, GET /preview (Phase 1C).

GET /start renders the picker, sourcing its title/company options from the
live corpus (jobcannon.db._feed.distinct_titles / distinct_companies —
never a hardcoded list) so the options can never drift from what the
database actually contains. A corpus that skews heavily toward a handful of
alphabetically-early titles otherwise buries every other real title outside
the unfiltered top-N window, so GET /start also accepts an optional `q`
search term (#148) that narrows both fieldsets server-side (ILIKE,
prefix-match ranked first — jobcannon.db._feed._distinct_matching); the
picker's own search input hx-gets this SAME route (gated on the
`HX-Request` header — see the `start` view for the fragment-vs-full-page
split) with a plain `?q=` GET as the no-JS fallback. Once a picker
submission is pending in the session, GET /start instead renders a
"submitted" confirmation state linking to GET /preview — an in-scope,
independently-tested render, not a placeholder for a later PR.

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

from jobcannon.db._feed import (
    FEED_PAGE_MAX,
    cursor_from_row,
    distinct_companies,
    distinct_titles,
    list_feed_postings,
    parse_cursor,
)
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

# profiles.comp_floor_usd (m0008) is a Postgres `integer` (int4) column, so
# this bound is the column's own domain boundary, not an arbitrary business
# policy: a value above int4's max would reach upsert_profile and raise
# psycopg.errors.NumericValueOutOfRange, an unhandled 500 rather than the
# 200 re-render every other validation failure on this route gets — same
# boundary-enforcement role MAX_YEARS_OF_EXPERIENCE plays for its own
# column (issue #28 item 2).
MAX_COMP_FLOOR_USD = 2_147_483_647

# Title selections are corpus-derived but deliberately NOT membership-checked
# against the rendered option window (a legitimate title outside the current
# top-N window must remain selectable — see the module docstring). POST
# /start still accepts an arbitrary form body regardless of what the picker
# actually rendered, so nothing else bounds a submission's size before it
# reaches upsert_profile's target_titles jsonb column (issue #54).
#
# Both bounds are sized against a second, tighter constraint than Postgres:
# every accepted selection also round-trips through set_pending_picker into
# the signed Flask session COOKIE (client-side storage), and RFC 6265
# browsers commonly support only ~4093 bytes per cookie — Werkzeug warns and
# the browser silently drops the cookie past that, which would degrade
# /preview to "no selections" for a real visitor. Measured empirically
# against this route (itsdangerous zlib+base64 non-repeating text roughly
# breaks even byte-for-byte): MAX_TITLES_PER_SELECTION titles at exactly
# MAX_TITLE_LENGTH chars each serializes to ~3.3KB, leaving headroom under
# the ~4093-byte ceiling for the picker's other session-held fields. Sizing
# to the corpus's own `distinct_titles(limit=50)` option-window instead would
# put the same worst case over 10KB — verified against this exact route
# during review, not merely calculated.
MAX_TITLES_PER_SELECTION = 20

# postings.title has no DB-level length constraint (`text`), so this is a
# policy choice, not an observed corpus maximum — sized against this
# repo's own established plausible-title-length precedent
# (jobcannon/engine/careers_crawler/_title_filters.py's `_MAX_TITLE_LEN = 140`:
# "Real titles top out around 110 chars even with senior/staff/principal
# modifiers + parenthesized scopes. Beyond 140 the candidate is almost
# certainly a metadata blob.") rather than an arbitrarily larger "generous"
# number — see MAX_TITLES_PER_SELECTION's comment for why a bigger bound
# would break the session-cookie round trip anyway.
MAX_TITLE_LENGTH = 140

# `companies` selections never reach durable storage (there is no
# target_companies column anywhere in this schema — see the comment at the
# `companies` extraction site in _parse_submission below), but they DO
# share the exact same signed-cookie round trip titles take via
# set_pending_picker, so an uncapped `companies` submission can blow the
# same ~4093-byte RFC 6265 ceiling independently of titles (issue #80,
# filed during #54's review). Capped with the same shape (count + per-item
# length) MAX_TITLES_PER_SELECTION/MAX_TITLE_LENGTH use, but the split
# between the two is sized differently, LENGTH first: unlike titles (bounded
# upstream by careers_crawler's `_MAX_TITLE_LEN = 140`, so the picker's
# rendered option window can never contain a title long enough to exceed
# MAX_TITLE_LENGTH — see that constant's comment), postings.company /
# companies.name have NO upstream length bound anywhere in this codebase —
# so, unlike titles, no finite MAX_COMPANY_LENGTH can guarantee every
# rendered option stays selectable; some cap is unavoidable given the
# cookie budget below, and this is a known, accepted residual limitation,
# not a solved one. Two earlier passes were rejected on two DIFFERENT
# grounds: 10 x 60 (mirroring titles' own proportions) measured OVER the
# session-cookie budget (4235 bytes against the ~4093 ceiling — see
# MAX_COMPANY_LENGTH's comment for the full combined measurement); 8 x 35
# fit the budget but was low enough on length to plausibly reject a real
# rendered corpus name (see MAX_COMPANY_LENGTH's comment for concrete
# examples over 35 chars) — the same functional defect #76 avoided for
# titles by aligning with its upstream bound, not achievable here since
# there is no upstream bound to align with. MAX_COMPANY_LENGTH is set
# first, as high as the remaining budget allows once titles + the cookie's
# other fixed costs are accounted for; this constant is then whatever
# selection COUNT that length leaves room for — a usability consequence of
# the budget, not an assumption that nobody would ever want more.
# distinct_companies(limit=50) still renders up to 50 selectable options,
# same as titles.
MAX_COMPANIES_PER_SELECTION = 5

# Sized first (see MAX_COMPANIES_PER_SELECTION's comment for why length
# leads here, and for the residual limitation this bound does NOT close):
# 55 chars covers the overwhelming majority of real company legal names,
# suffixes included (e.g. "PricewaterhouseCoopers International Limited" is
# 46 chars, "Federal Home Loan Mortgage Corporation" is 39) — a corpus entry
# longer than that remains unselectable through the picker, same tradeoff
# the cookie budget forces onto every option here. Then measured
# empirically (same non-repeating-text methodology #76 used for titles —
# itsdangerous's zlib+base64 session encoding roughly breaks even
# byte-for-byte on non-repeating text, so a compressible degenerate input
# would understate the true worst case) with the actual production cookie
# attributes (SESSION_COOKIE_SECURE=True adds "; Secure" that a TESTING app
# doesn't emit — this route's own tests must build that in, not measure the
# testing-only shape): one POST /start carrying titles at
# MAX_TITLES_PER_SELECTION x MAX_TITLE_LENGTH, companies at
# MAX_COMPANIES_PER_SELECTION x MAX_COMPANY_LENGTH, every other
# pending_picker field at ITS own worst case (all 10 SKILLS_OPTIONS tokens,
# the longest SENIORITY_LEVELS value "principal", a workplace_type token),
# AND a worst-case `?ref=` channel (_CHANNEL_MAX_LEN=32 chars) plus a
# worst-case Referer host (events_schema._MAX_STR=200 chars — both
# visitor-controlled and captured into the same cookie by
# anon_session.py's capture_attribution/ensure_session_ids on this same
# first request, alongside anon_session_id/feed_session_id) serializes to
# ~3910 bytes total (a few bytes of run-to-run jitter from mint_anon_user's
# random anon_id affecting zlib's compression ratio; observed range across
# 5 runs: 3906-3911) — under the common ~4093-byte browser per-cookie
# ceiling with ~180 bytes (~4.4%) to spare even at the high end. For scale:
# titles alone at cap plus that same worst-case attribution tail (i.e. this
# exact scenario minus `companies` entirely) already measures 3620 bytes —
# the attribution tail is a pre-existing cost, not one this PR introduced,
# but it's why `companies` doesn't get titles' full proportional share of
# the remaining budget. Verified against this exact route in
# tests/host/test_onboarding.py::test_company_selections_at_the_cap_boundary_write_successfully,
# not merely calculated.
MAX_COMPANY_LENGTH = 55

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


def _corpus_options(conn: Any, q: str) -> dict[str, list[str]]:
    return {
        "titles": distinct_titles(conn, q=q or None),
        "companies": distinct_companies(conn, q=q or None),
    }


def _read_picker_options(q: str = "") -> dict[str, list[str]]:
    """Fail-closed corpus read, mirroring jobcannon/web/pages.py's
    _read_page_data shape: an unopened pool or a genuine DB outage degrades
    to empty title/company option lists (the picker still renders — its
    fixed-enum fields are unaffected) rather than a 500 on the public
    onboarding entry point. `q` (#148) narrows both option lists through
    jobcannon.db._feed's ILIKE search; empty/None means the original
    unfiltered, alphabetical, corpus-derived window."""
    try:
        with connection_factory() as conn:
            return _corpus_options(conn, q)
    except Exception:
        logger.warning(
            "picker option read failed (defaulting to empty corpus options)", exc_info=True
        )
        return dict(_EMPTY_OPTIONS)


def _merge_checked(options: list[str], checked: list[str]) -> list[str]:
    """`options` (the corpus search/window result, already ordered) with any
    already-checked value not present in that window appended at the end
    (#148) — so a selection made before a narrower search term, or before a
    validation-error re-render, never silently disappears from the rendered
    fieldset. Not coincidentally, this is also what lets the fieldset carry
    forward a `checked` attribute at all: before this, GET /start had no
    state-restoration mechanism of any kind, so any in-page re-render (the
    search box's own hx-get swap being the first one this route ever had)
    would have silently unchecked everything outside the new window."""
    seen = set(options)
    return [*options, *[value for value in checked if value not in seen]]


def _picker_context(
    *,
    error: str | None = None,
    q: str = "",
    checked_titles: list[str] | None = None,
    checked_companies: list[str] | None = None,
) -> dict[str, Any]:
    checked_titles = checked_titles or []
    checked_companies = checked_companies or []
    options = _read_picker_options(q)
    return {
        "submitted": False,
        "error": error,
        "q": q,
        "titles": _merge_checked(options["titles"], checked_titles),
        "companies": _merge_checked(options["companies"], checked_companies),
        "checked_titles": checked_titles,
        "checked_companies": checked_companies,
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


def _parse_companies(form: Any) -> tuple[list[str] | None, str | None]:
    """Shape-validate the submitted company selections before they reach
    set_pending_picker's session cookie: count cap, per-item length cap, and
    a type check — the same shape _parse_titles enforces (issue #80). No
    control-character rejection here: unlike titles, companies never reach a
    durable jsonb column (see the comment at this function's call site), so
    the concern that check exists for doesn't apply — the cookie-budget caps
    below are the only hazard `companies` shares with `titles`."""
    raw_companies = [c for c in form.getlist("companies") if c]
    if len(raw_companies) > MAX_COMPANIES_PER_SELECTION:
        return None, f"too many companies selected (max {MAX_COMPANIES_PER_SELECTION})"

    companies: list[str] = []
    for company in raw_companies:
        if not isinstance(company, str):
            return None, "companies must be text values"
        if len(company) > MAX_COMPANY_LENGTH:
            return None, f"company exceeds the {MAX_COMPANY_LENGTH}-character limit"
        companies.append(company)
    return companies, None


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

    # #28 item 2: optional numeric input, same validation shape as
    # years_of_experience above (try/except -> type error, then a range
    # check -> range error, both re-rendering the form with 200). Parsed
    # with int() rather than float(): comp_floor_usd is a whole-dollar
    # `integer` column (m0008), not a fractional numeric one, so a
    # decimal input (e.g. "120000.50") is rejected outright rather than
    # silently truncated.
    comp_floor_raw = (form.get("comp_floor_usd") or "").strip()
    comp_floor_usd: int | None = None
    if comp_floor_raw:
        try:
            comp_floor_usd = int(comp_floor_raw)
        except ValueError:
            return None, "compensation floor must be a whole number"
        if not (0 <= comp_floor_usd <= MAX_COMP_FLOOR_USD):
            return None, (f"compensation floor must be between 0 and {MAX_COMP_FLOOR_USD:,}")

    titles, error = _parse_titles(form)
    if error is not None:
        return None, error

    companies, error = _parse_companies(form)
    if error is not None:
        return None, error

    selections = {
        "titles": titles,
        # `companies` selections never reach durable storage today — there
        # is no target_companies column anywhere in this schema (upsert_profile
        # doesn't accept one); this list only flows into the session via
        # set_pending_picker for /preview's read-side filter. Still
        # shape-validated (count + length cap, see MAX_COMPANIES_PER_SELECTION)
        # because it round-trips through the same session cookie titles do —
        # a durable sink would additionally need titles' control-character
        # check.
        "companies": companies,
        "skills": [s for s in form.getlist("skills") if s and s in SKILLS_OPTIONS],
        "seniority_level": seniority_level,
        "years_of_experience": years_of_experience,
        # #28 item 2: validated here (not re-parsed at the upsert_profile
        # call site) so every submission goes through exactly one
        # validation path, same as every other field above. Deliberately
        # NOT spread into pending_picker's session cookie by start_submit
        # below — unlike titles/skills/seniority_level/years_of_experience/
        # workplace_type, nothing on GET /preview reads a compensation
        # floor (it exists purely for the host scoring path's comp_fit
        # anchoring). Filtering the key out unconditionally means this
        # field changes the session-cookie payload not at all — verified by
        # rerunning test_company_selections_at_the_cap_boundary_write_
        # successfully unmodified (MAX_COMPANY_LENGTH's module comment
        # documents that boundary is already tight), not just reasoned about.
        "comp_floor_usd": comp_floor_usd,
        "workplace_type": _WORKPLACE_FILTERS[workplace_type],
    }
    return selections, None


@onboarding_bp.get("/start", strict_slashes=False)
def start():
    """#148: `q` (an optional search term) narrows the Titles/Companies
    fieldsets server-side. The exact same computation serves two shapes,
    gated on the `HX-Request` header — the picker's search input hx-gets
    THIS route, so the two can never drift out of sync the way a separate
    fragment endpoint could: an `HX-Request` returns just the fieldsets
    (`_picker_options.html`, swapped into `#picker-options`); anything else
    — a direct browser hit on `/start?q=...`, or a JS-disabled visitor
    submitting the search form's own GET fallback — gets the full page with
    those same filtered fieldsets already rendered. `titles`/`companies`
    query params (present when the search box's hx-include carries forward
    the visitor's already-checked boxes) are read here too, capped the same
    way POST /start caps a submission, so a pathological URL can't force an
    unbounded render."""
    pending = get_pending_picker()
    if pending is not None:
        return render_template("onboarding_picker.html", submitted=True, pending=pending)

    q = (request.args.get("q") or "").strip()
    checked_titles = request.args.getlist("titles")[:MAX_TITLES_PER_SELECTION]
    checked_companies = request.args.getlist("companies")[:MAX_COMPANIES_PER_SELECTION]
    context = _picker_context(
        q=q, checked_titles=checked_titles, checked_companies=checked_companies
    )
    if request.headers.get("HX-Request") == "true":
        return render_template("_picker_options.html", **context)
    return render_template("onboarding_picker.html", **context)


@onboarding_bp.post("/start", strict_slashes=False)
def start_submit():
    selections, error = _parse_submission(request.form)
    if error is not None:
        checked_titles = request.form.getlist("titles")[:MAX_TITLES_PER_SELECTION]
        checked_companies = request.form.getlist("companies")[:MAX_COMPANIES_PER_SELECTION]
        context = _picker_context(
            error=error, checked_titles=checked_titles, checked_companies=checked_companies
        )
        return render_template("onboarding_picker.html", **context), 200

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
                comp_floor_usd=selections["comp_floor_usd"],
            )

    # comp_floor_usd is deliberately excluded from the session payload — see
    # the comment at its validation site in _parse_submission above.
    pending_selections = {k: v for k, v in selections.items() if k != "comp_floor_usd"}
    set_pending_picker({"anon_id": anon_id, **pending_selections})
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
    *,
    titles: list[str] | None,
    workplace_type: str | None,
    location_contains: str | None,
    after: tuple[float | None, Any, int] | None,
) -> list[Any]:
    """Fail-closed corpus read, same shape as _read_picker_options: an
    unopened pool or a genuine DB outage degrades to an empty result list
    (_feed_list.html's empty-state branch still renders) rather than a 500
    on a public entry point. `after` (#156) is the keyset cursor for
    "Load more" — see jobcannon/db/_feed.py's list_feed_postings/
    _cursor_predicate."""
    try:
        with connection_factory() as conn:
            return list_feed_postings(
                conn,
                user_id=None,
                titles=titles,
                workplace_type=workplace_type,
                location_contains=location_contains,
                after=after,
            )
    except Exception:
        logger.warning("preview feed read failed (defaulting to empty result set)", exc_info=True)
        return []


def _preview_load_more_url(location_contains: str | None, rows: list[Any]) -> str | None:
    """Next-page URL for /preview's "Load more" control, or None when this
    page came back short of FEED_PAGE_MAX — a keyset page shorter than the
    cap proves there is nothing left to seek past (#156). `titles`/
    `workplace_type` need no explicit carry-forward here: they come from the
    session-held picker selections (get_pending_picker), which every request
    on this route already re-reads identically, unlike `location`, which is
    this route's own query-string filter and must round-trip through the
    URL the same way jobcannon/web/pages.py's `_feed_load_more_url` carries
    its filters forward."""
    if len(rows) < FEED_PAGE_MAX:
        return None
    query = {"location": location_contains} if location_contains else {}
    return url_for("onboarding.preview", **query, **cursor_from_row(rows[-1]))


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
    """#156: paginates via a keyset cursor read from `cursor_id`/
    `cursor_last_seen`/`cursor_rank_score` query params (jobcannon.db._feed.
    parse_cursor — a malformed/tampered value degrades to a first page, not
    a 500). Same HX-Request split as GET /start (#148): the "Load more"
    button hx-gets THIS route with the next cursor attached, so an
    HX-Request returns only the next batch (+ a further "Load more", or
    nothing when exhausted — `_feed_page.html`); a direct browser hit — the
    first real GET /preview, or someone opening a "Load more" URL straight
    in a new tab — gets the full page, at whatever page the cursor names."""
    if _current_identity() is not None:
        return redirect(url_for("pages.feed"))

    pending = get_pending_picker()
    selections: dict[str, Any] = pending if pending is not None else {}
    location_contains = (request.args.get("location") or "").strip() or None
    after = parse_cursor(request.args)

    rows = _read_preview_postings(
        titles=selections.get("titles") or None,
        workplace_type=selections.get("workplace_type"),
        location_contains=location_contains,
        after=after,
    )
    entries = [{"row": row, "chips": why_chips(row, selections)} for row in rows]
    load_more_url = _preview_load_more_url(location_contains, rows)

    if request.headers.get("HX-Request") == "true":
        return render_template("_feed_page.html", entries=entries, load_more_url=load_more_url)

    return render_template(
        "preview.html",
        entries=entries,
        has_selections=bool(selections),
        load_more_url=load_more_url,
        ordering=_ordering_label(rows),
        location_contains=location_contains or "",
    )
