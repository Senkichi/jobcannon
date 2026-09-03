# PORTED from job_finder/web/location_fit.py @ 6fd9f9b31c6a32c7262de3619d247008425e2cde (private job-cannon). Ledger L-0195.
"""Deterministic location_fit override from structured location facts (P3.1).

Design rule D-6: "Facts beat judgment." Geography membership, remote
eligibility, and country exclusion are *deterministic* facts derivable from
``locations_structured`` + ``primary_country_code`` + ``workplace_type`` +
the candidate's ``target_locations`` and ``home_country``. This module
computes them in Python and returns an override score (int 1–5) + rationale
string that replaces the LLM-emitted ``location_fit`` sub-score when the
facts decide the outcome unambiguously.

The override runs **post-LLM, pre-persist** in the scoring orchestrator
(``score_and_persist_job``): schema unchanged, ``derive_classification``
unchanged, no prompt change → **no eval gate needed**. The eval harness
measures the model; this override is downstream policy that applies
deterministically on top.

Rule table — first matching row wins; returns ``None`` when no row fires
(the LLM judgment is authoritative for the undecided cases):

    Row 1: any REMOTE location, unrestricted, 'Remote' ∈ targets
           → (5, "fully remote, remote targeted")
    Row 2: any REMOTE location restricted to home_country     [†home_country]
           → (5, "fully remote, remote targeted")
    Row 3: all REMOTE locations restricted to countries ≠ home_country [†]
           → (1, "remote but ineligible geography")
    Row R-a: work_arrangement=="remote" AND a presence-required (hybrid/onsite)
           location matches a non-Remote target
           → (4, "on-site/hybrid in target geography, remote preferred")
    Row R-b: work_arrangement=="remote" AND every location is presence-required
           (hybrid/onsite) with known geo AND none matches a target
           → (1, "on-site/hybrid outside target geography")
    Row 4: all locations onsite/hybrid/UNSPECIFIED, countries ≠ home_country,
           no target_location matches any city/region             [†]
           → (1, "on-site outside candidate geography")
    Row 5: any location's city/region/country matches a non-Remote target
           → (5, "on-site/hybrid in target geography")
    → None: LLM judges (e.g. onsite home-country city not in targets)

Rows R-a/R-b fire only for a remote-first candidate (``work_arrangement ==
"remote"``); when the candidate prefers hybrid/on-site (or the preference is
unset) the legacy rows 4/5 apply and an on-site/hybrid role in a target
geography is a full 5.

Multi-location rule: best location wins — a job offerable in NYC *or* Toronto
is as good as its best option for the candidate.
Unresolved entries (``unresolved=True``) contribute nothing.

†-rows fire only when ``home_country`` is present (non-None, non-empty).

Reference: issue #390, §P3.1.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Shared vocabulary for the candidate's work-arrangement preference (config
# profile.work_arrangement) and the corresponding jd-side workplace_type
# values stored in the DB. Lowercase; DB column uses uppercase equivalents
# (REMOTE, HYBRID, ONSITE). Keep in sync with #390's rule table.
VALID_WORK_ARRANGEMENTS: frozenset[str] = frozenset({"remote", "hybrid", "on-site"})

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _norm(value: str | None) -> str:
    """Normalize a string for case-insensitive comparison."""
    return (value or "").strip().lower()


def _remote_unrestricted(loc: dict[str, Any]) -> bool:
    """True iff a location dict represents an unrestricted remote posting.

    "Unrestricted" means the location carries no country constraint — the
    country_code is None/empty. A REMOTE posting with an explicit country
    (e.g. ``{workplace_type: "REMOTE", country_code: "US"}``) is
    *country-restricted* and falls through to row 2 / row 3.
    """
    return _norm(loc.get("workplace_type")) == "remote" and not loc.get("country_code")


def _remote_in_country(loc: dict[str, Any], country_code: str) -> bool:
    """True iff the location is REMOTE and restricted to ``country_code``."""
    return _norm(loc.get("workplace_type")) == "remote" and _norm(
        loc.get("country_code")
    ) == _norm(country_code)  # fmt: skip  # PORT-SEAM: wrap pinned to match private source; ruff would otherwise re-wrap and false-flag fidelity-diff


def _remote_outside_country(loc: dict[str, Any], country_code: str) -> bool:
    """True iff the location is REMOTE and restricted to a country ≠ home."""
    cc = loc.get("country_code")
    return (
        _norm(loc.get("workplace_type")) == "remote"
        and bool(cc)  # must have a country restriction to be "outside"
        and _norm(cc) != _norm(country_code)
    )


def _is_remote(loc: dict[str, Any]) -> bool:
    """True iff location has any REMOTE workplace_type."""
    return _norm(loc.get("workplace_type")) == "remote"


def _onsite_or_hybrid_or_unspecified(loc: dict[str, Any]) -> bool:
    """True iff location is onsite/hybrid/UNSPECIFIED (non-remote)."""
    wt = _norm(loc.get("workplace_type"))
    return wt in ("onsite", "hybrid", "unspecified", "")


def _country_outside_home(loc: dict[str, Any], home_country_code: str) -> bool:
    """True iff location has a country that differs from home_country."""
    cc = loc.get("country_code")
    if not cc:
        # No country info — cannot confirm it is "outside"; treat as ambiguous.
        return False
    return _norm(cc) != _norm(home_country_code)


def _target_loc_matches(loc: dict[str, Any], target_locations: list[str]) -> bool:
    """True iff any non-Remote target_location matches the city, region, or country.

    Matching is case-insensitive substring/equality. The "Remote" token is
    explicitly excluded — it is a modality signal, not a geography membership
    test.  Row 5 fires on a geographic match; row 1 already covers the pure
    remote case.
    """
    geo_targets = [t for t in target_locations if _norm(t) != "remote"]
    city = _norm(loc.get("city"))
    region = _norm(loc.get("region"))
    country = _norm(loc.get("country"))

    for target in geo_targets:
        t = _norm(target)
        if not t:
            continue
        # Substring match: target "San Francisco" matches city "San Francisco",
        # and "California" matches region "California". Also handles cases
        # where the target is a country ("United States") matching country.
        if city and t in city:
            return True
        if region and t in region:
            return True
        if country and t in country:
            return True
        # Reverse check: city/region/country substring in target (e.g. target
        # "New York, NY" and city "New York").
        if city and city in t:
            return True
        if region and region in t:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_location_fit(
    locations_structured: list[dict[str, Any]],
    workplace_type: str | None,
    primary_country_code: str | None,
    target_locations: list[str],
    home_country: str | None,
    *,
    work_arrangement: str | None = None,
    has_subcountry_constraint: bool = False,
) -> tuple[int, str] | None:
    """Deterministic location_fit when structured facts decide it; None → LLM judges.

    Args:
        locations_structured: Parsed JobLocation objects serialized to dicts
            (the ``locations_structured`` DB column, decoded from JSON).
            Each dict has: city, region, region_code, country, country_code,
            workplace_type, raw, unresolved. Entries with ``unresolved=True``
            contribute nothing.
        workplace_type: Denormalized ``jobs.workplace_type`` column value
            (e.g. "REMOTE", "ONSITE", "HYBRID", "UNSPECIFIED"). Used as a
            fallback when ``locations_structured`` is empty.
        primary_country_code: Denormalized ``jobs.primary_country_code`` column
            value. Used as a fallback for country when ``locations_structured``
            is empty.
        target_locations: Candidate's ``profile.target_locations`` list from
            config. Typically contains items like "Remote", "San Francisco",
            "New York".
        home_country: Candidate's ``profile.home_country`` ISO country code
            (e.g. "US"). Optional; rows marked † in the rule table require
            this to fire. When None/empty, those rows are silently skipped.
        work_arrangement: Candidate's ``profile.work_arrangement`` preference
            ("remote" | "hybrid" | "on-site"). Optional. When ``"remote"``,
            the remote-first refinement (rows R-a/R-b) fires: a role that
            REQUIRES physical presence (hybrid/onsite) can never equal a fully
            remote role, so it is capped at 4 in a target geography and is a
            disqualifier (1) outside one. When None or non-remote, the legacy
            rows 4/5 apply unchanged (on-site/hybrid in a target geography is a
            5 — the candidate wants to be there).
        has_subcountry_constraint: #1202 — when True, the JD carries a
            geographic/residency constraint FINER than country/region/city
            (e.g. a remote role restricted to a named subset of US states,
            excluding others) that the ``locations_structured`` schema cannot
            represent. The rule table would mis-fire (e.g. Row 2 "REMOTE
            restricted to home_country" → 5) because the structured facts
            under-express the constraint. Short-circuit to ``None`` so the
            LLM's own judgment is authoritative — the function's documented
            behavior for facts it cannot decide. This is a gate on the
            existing override, not a parallel scoring path.

    Returns:
        ``(score: int, reason: str)`` when facts are decisive, ``None`` when
        the LLM should judge (ambiguous / insufficient data).

    Rule table (first match wins; D-6 — D-10 cite):
        Row 1: any REMOTE, unrestricted, 'Remote' ∈ targets
               → (5, "fully remote, remote targeted")
        Row 2: any REMOTE restricted to home_country        [† needs home_country]
               → (5, "fully remote, remote targeted")
        Row 3: all REMOTE restricted to countries ≠ home_country [†]
               → (1, "remote but ineligible geography")
        Row R-a: work_arrangement=="remote" AND any presence-required
               (hybrid/onsite) location matches a non-Remote target
               → (4, "on-site/hybrid in target geography, remote preferred")
        Row R-b: work_arrangement=="remote" AND every location is
               presence-required (hybrid/onsite) with known geo AND none
               matches a target
               → (1, "on-site/hybrid outside target geography")
        Row 4: all onsite/hybrid/UNSPECIFIED in countries ≠ home_country,
               no target_loc city/region match              [†]
               → (1, "on-site outside candidate geography")
        Row 5: any city/region/country matches a non-Remote target
               → (5, "on-site/hybrid in target geography")
        otherwise → None

    Rows R-a/R-b encode the remote-first preference: the candidate-context
    prompt already tells the model "a remote role is preferred over any
    on-site/hybrid target-geo role", but the deterministic override used to
    flatten hybrid-in-target back up to 5 (equal to remote), erasing that
    gradient and — worse — riding a secondary target-city match to a 5 for a
    posting the candidate reads as an out-of-target hybrid. UNSPECIFIED-modality
    locations stay ambiguous (they could be remote) and never trigger R-b's
    reject; they fall through to rows 4/5/LLM.
    """
    target_locations = target_locations or []
    home = (home_country or "").strip().upper() or None

    # #1202: sub-country constraint gate. When the JD carries a geographic
    # constraint finer than country/region/city (e.g. a remote role restricted
    # to a named subset of US states), the structured facts under-express it
    # and the rule table would mis-fire (Row 2 "REMOTE restricted to
    # home_country" → 5 for a posting the candidate cannot actually take).
    # Short-circuit to None — the LLM's own judgment is authoritative for
    # facts the deterministic table cannot decide. This is a gate on the
    # existing override, not a parallel scoring path.
    if has_subcountry_constraint:
        return None

    # Build the resolved (non-unresolved) location list.
    resolved: list[dict[str, Any]] = [
        loc for loc in (locations_structured or []) if not loc.get("unresolved")
    ]

    # When locations_structured is empty or all unresolved, synthesize a
    # single pseudo-entry from the denormalized columns so the rules can
    # still fire on the available data (e.g. the data_enricher LLM extract
    # path that writes workplace_type/primary_country_code before the full
    # structured parse runs).
    if not resolved and (workplace_type or primary_country_code):
        resolved = [
            {
                "workplace_type": (workplace_type or "UNSPECIFIED").upper(),
                "country_code": primary_country_code or None,
                "city": None,
                "region": None,
                "country": None,
                "unresolved": False,
            }
        ]

    if not resolved:
        # No structured facts at all — LLM judges.
        return None

    remote_in_targets = any(_norm(t) == "remote" for t in target_locations)
    prefers_remote = _norm(work_arrangement) == "remote"
    job_modality = _norm(workplace_type)

    def _eff_modality(loc: dict[str, Any]) -> str:
        """Effective workplace modality for one location.

        Prefers the location's own ``workplace_type``; when that is
        UNSPECIFIED/blank, falls back to the denormalized job-level
        ``workplace_type``. The structured parser sometimes captures only one
        city of a multi-city posting and drops its per-location modality (the
        Brigit "Lead Data Scientist" row: structured SF marked UNSPECIFIED while
        the job column still says HYBRID), so this fallback keeps the remote-first
        rows from silently missing a presence-required role. Returns "" when the
        modality is genuinely unknown at both levels.
        """
        wt = _norm(loc.get("workplace_type"))
        if wt in ("remote", "hybrid", "onsite"):
            return wt
        return job_modality if job_modality in ("remote", "hybrid", "onsite") else ""

    # ------------------------------------------------------------------
    # Row 1: any REMOTE, unrestricted (no country), 'Remote' ∈ targets
    # ------------------------------------------------------------------
    if remote_in_targets and any(_remote_unrestricted(loc) for loc in resolved):
        return (5, "fully remote, remote targeted")

    # ------------------------------------------------------------------
    # Row 2: any REMOTE restricted to home_country       [†home_country]
    # "Restricted to home_country" means country_code == home — the job is
    # remote but only for residents of the candidate's country.
    # ------------------------------------------------------------------
    if home and remote_in_targets:
        if any(_remote_in_country(loc, home) for loc in resolved):
            return (5, "fully remote, remote targeted")

    # ------------------------------------------------------------------
    # Row 3: ALL remote locations are restricted to countries ≠ home_country
    # Fires only when every location is REMOTE AND every REMOTE has an
    # explicit country ≠ home. A mix of REMOTE+onsite, or any unrestricted
    # REMOTE, falls through.
    # ------------------------------------------------------------------
    if home:
        remote_locs = [loc for loc in resolved if _is_remote(loc)]
        if remote_locs and all(_remote_outside_country(loc, home) for loc in remote_locs):
            # Only fires when ALL resolved are remote-and-outside — no onsite
            # fallback location exists that could override.
            non_remote = [loc for loc in resolved if not _is_remote(loc)]
            if not non_remote:
                return (1, "remote but ineligible geography")

    # ------------------------------------------------------------------
    # Rows R-a / R-b: remote-first refinement (work_arrangement == "remote").
    # A fully remote option would already have returned 5 at Row 1/2, so by here
    # no remote-eligible location exists. A role that REQUIRES physical presence
    # (hybrid/onsite) is therefore a compromise even in a target geo, and
    # unreachable outside one:
    #   R-a: any presence-required location in a target geography → 4
    #        (viable, but ranked strictly below a fully remote role)
    #   R-b: every location presence-required, all with known geo, none in a
    #        target geography → 1 (unreachable for a remote-first candidate)
    # UNSPECIFIED-modality locations stay ambiguous (could be remote) and never
    # trigger the reject — they fall through to rows 4/5/LLM.
    # ------------------------------------------------------------------
    if prefers_remote:
        presence_locs = [loc for loc in resolved if _eff_modality(loc) in ("hybrid", "onsite")]
        if presence_locs:
            if any(_target_loc_matches(loc, target_locations) for loc in presence_locs):
                return (4, "on-site/hybrid in target geography, remote preferred")
            all_presence = len(presence_locs) == len(resolved)
            geo_known = all(
                loc.get("city") or loc.get("region") or loc.get("country") for loc in presence_locs
            )
            if all_presence and geo_known:
                return (1, "on-site/hybrid outside target geography")

    # ------------------------------------------------------------------
    # Row 4: all locations onsite/hybrid/UNSPECIFIED, every one has a country
    # ≠ home_country, AND no target_location city/region matches   [†]
    # ------------------------------------------------------------------
    if home:
        non_remote_locs = [loc for loc in resolved if _onsite_or_hybrid_or_unspecified(loc)]
        if non_remote_locs and len(non_remote_locs) == len(resolved):
            # Every resolved location is non-remote.
            all_outside = all(_country_outside_home(loc, home) for loc in non_remote_locs)
            if all_outside:
                no_geo_match = not any(
                    _target_loc_matches(loc, target_locations) for loc in non_remote_locs
                )
                if no_geo_match:
                    return (1, "on-site outside candidate geography")

    # ------------------------------------------------------------------
    # Row 5: any location's city/region/country matches a non-Remote target
    # ------------------------------------------------------------------
    if any(_target_loc_matches(loc, target_locations) for loc in resolved):
        return (5, "on-site/hybrid in target geography")

    # No rule fired — LLM judges (e.g. onsite home-country city not in targets:
    # desirability requires judgment).
    return None


def compute_posting_fits(
    postings: list[dict[str, Any]],
    target_locations: list[str],
    home_country: str | None,
    work_arrangement: str | None,
    *,
    has_subcountry_constraint: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    """Compute per-posting location_fit and return updated postings + row rollup.

    For each posting descriptor in the row's ``postings`` list (the JSON column
    added in P1), calls ``compute_location_fit`` with that posting's own
    ``locations_structured`` (list of dicts), its ``workplace_type``, and
    ``primary_country_code`` derived from its own locations (first location's
    ``country_code``).

    When ``compute_location_fit`` returns ``None`` for a posting (LLM-undecided
    facts), leaves that posting's ``location_fit`` unset/``None`` rather than
    fabricating a number.

    ``has_subcountry_constraint`` (#1202) is a row-level gate forwarded to
    every per-posting ``compute_location_fit`` call — when True, every posting
    short-circuits to ``None`` (the JD carries a constraint the structured
    facts cannot represent).

    Returns:
        ``(updated_postings, rollup)`` where:
        - ``updated_postings``: new posting dicts with ``location_fit`` written
          on each (immutability — never mutates the input list)
        - ``rollup``: the maximum of the resolved per-posting ``location_fit``
          values (best door wins). If every posting is ``None`` (all facts-
          undecided), the rollup is ``None`` and the row override must not fire
          (LLM judgment stays authoritative).

    Applies the same ``target_locations`` "remote" sentinel adaptation the row
    override already does (``scoring_orchestrator.py:206-212``) so remote-first
    candidates are judged consistently per posting.

    Reference: issue #642, P3.
    """
    target_locations = target_locations or []
    # When the candidate targets remote work, synthesize the "remote" sentinel so
    # compute_location_fit's Row 1/2 remote-eligibility checks still fire correctly.
    # This mirrors the adaptation in _apply_location_fit_override.
    if work_arrangement == "remote" and not any(
        (t or "").strip().lower() == "remote" for t in target_locations
    ):
        target_locations = ["remote"] + target_locations

    updated_postings: list[dict[str, Any]] = []
    resolved_fits: list[int] = []

    for posting in postings:
        # Build a new dict for immutability
        new_posting = dict(posting)

        locations_structured = posting.get("locations_structured")
        workplace_type = posting.get("workplace_type")
        # Derive primary_country_code from first location's country_code
        # (mirror job_finder/db/_jobs.py:468-469)
        primary_country_code: str | None = None
        if (
            locations_structured
            and isinstance(locations_structured, list)
            and len(locations_structured) > 0
        ):
            first_loc = locations_structured[0]
            if isinstance(first_loc, dict):
                primary_country_code = first_loc.get("country_code")

        verdict = compute_location_fit(
            locations_structured=locations_structured or [],
            workplace_type=workplace_type,
            primary_country_code=primary_country_code,
            target_locations=target_locations,
            home_country=home_country,
            work_arrangement=work_arrangement,
            has_subcountry_constraint=has_subcountry_constraint,
        )

        if verdict is not None:
            new_score, _reason = verdict
            new_posting["location_fit"] = new_score
            resolved_fits.append(new_score)
        # Else: leave location_fit unset (None) when facts are undecided

        updated_postings.append(new_posting)

    # Row rollup = maximum of resolved per-posting fits (best door wins)
    rollup: int | None = max(resolved_fits) if resolved_fits else None

    return updated_postings, rollup
