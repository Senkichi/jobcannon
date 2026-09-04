"""PORTED from job_finder/web/scoring_orchestrator.py @ 6fd9f9b31c6a32c7262de3619d247008425e2cde
(private job-cannon). Ledger L-0259.

Scoring orchestration -- v3.0 unified entry (Phase 34 Plan 4), hosted wiring.

Residence is host, not engine: this module calls
``jobcannon.db._assessment_writer.persist_job_assessment``, a ``jobcannon.db``
import the engine's DI rule forbids inside ``jobcannon/engine/`` (see
``_assessment_writer.py`` lines 16-18 and ``job_scorer.py``'s own module
docstring, both of which name a future *host* wiring of
``score_and_persist_job`` as the intended caller). Injected into the
already-declared optional ``ScanServices.score_and_persist_job`` field via
``jobcannon/host/wiring.py::build_scan_services``.

Public API:
    score_and_persist_job(job, conn, config,
                          scorer_fn=None, *, run_id=None) -> ScoringResult | None
    load_scoring_profile(config) -> dict

These functions handle the core scoring + persistence logic. Callers remain
responsible for:
- Creating and closing DB connections (thread-safety patterns vary by caller)
- Session/batch progress tracking (dashboard-specific concern)
- Activity logging (caller-specific metadata)
- Enrichment (pipeline_runner-specific pre-scoring step)
- Exclusion filtering (caller decides when to filter)

The scorer_fn parameter allows callers to pass their own reference to the
scoring function, which preserves mock injection in tests (tests patch the
name in the caller's module namespace).
"""

import functools
import hashlib
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

# PORT-SEAM: `from job_finder.db import persist_job_assessment` ->
# `jobcannon.db._assessment_writer` (the direct module, not a package
# re-export -- jobcannon/db/__init__.py deliberately does not re-export
# persist_job_assessment). `from job_finder.db._jobs import set_postings`
# has no import here at all: set_postings has no public counterpart (see
# the call site below). `from job_finder.web import user_data_dirs` is also
# gone -- the profile now arrives via `config`, not a single-user disk
# file (design note Q-A). `apply_location_policy_to_postings` is unused for
# the same reason set_postings is gone; `verdict_to_json` is new (seam #2
# below).
from jobcannon.db._assessment_writer import persist_job_assessment
from jobcannon.engine.location_policy import compute_location_policy, verdict_to_json

logger = logging.getLogger(__name__)

# Memoized candidate context. The cache lives at module scope (one slot is
# enough for a single-user local app, but the dict structure leaves room for
# multi-config eval runs). Invalidation is automatic — the fingerprint hashes
# the relevant config slice, so any settings save or profile edit produces a
# new key.
_CONTEXT_CACHE: dict[str, str] = {}
_CONTEXT_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE_MAX = 8  # cap to avoid unbounded growth in eval sweeps


def load_scoring_profile(config: dict) -> dict:
    """Load the candidate's scoring profile from ``config``.

    Args:
        config: Application config dict. Reads ``config["profile"]``.

    Returns:
        Profile dict, or ``{}`` if absent.
    """
    # PORT-SEAM: private resolved a single-user disk path (
    # `config["scoring"]["profile_path"]` or `config["profile_path"]`,
    # defaulting to `user_data_dirs.profile_path()`) and delegated to
    # `profile_schema.load_profile()` for file I/O. A hosted, multi-tenant
    # caller has no single profile file to resolve a path for -- design
    # note Q-A's recommendation is to thread the profile through `config`
    # at `build_scan_services` time (the host owns per-user resolution;
    # this orchestrator stays a pure consumer). Whatever host caller
    # assembles `config` for a scoring call is responsible for placing the
    # candidate's full profile bundle (targeting + resume fields) at
    # `config["profile"]`; `_profile_path` (the private path-resolution
    # helper) has no public counterpart and is dropped entirely.
    return config.get("profile") or {}


def _context_fingerprint(config: dict) -> str:
    """Stable fingerprint of all inputs that affect the candidate context.

    Hashes the ``config["profile"]`` block (target titles / locations /
    floor / industries / exclusions / resume fields). Settings saves
    rebuild config["profile"], so the JSON content changes and the cache
    invalidates automatically — no manual flush required.
    """
    # PORT-SEAM: private also hashed the experience-profile file's mtime
    # (`os.path.getmtime(_profile_path(config))`) so a profile-file edit
    # invalidated the cache independently of a config rebuild. There is no
    # profile file on this host (see load_scoring_profile above) -- the
    # config dict is the sole source of truth, so the mtime term is
    # dropped; a profile edit is only ever visible via a new config["profile"]
    # value, which this hash already covers.
    cfg_profile = config.get("profile") or {}
    blob = json.dumps({"profile": cfg_profile}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()


def _resolve_candidate_context(config: dict) -> str:
    """Return the prompt-ready candidate-context block for this config.

    Memoized by ``_context_fingerprint(config)``. Cache invalidates when
    the relevant config slice changes. This is the production-path entry
    point; tests can still call ``build_candidate_context`` directly for
    unit-level assertions.
    """
    key = _context_fingerprint(config)
    with _CONTEXT_CACHE_LOCK:
        cached = _CONTEXT_CACHE.get(key)
        if cached is not None:
            return cached

    # Load + build OUTSIDE the lock. PORT-SEAM: private's rationale here was
    # "load_profile does file I/O, and we don't want to serialize unrelated
    # scorers behind a slow disk read" -- load_scoring_profile is now a pure
    # config read (no disk I/O), but build_candidate_context itself is still
    # nontrivial per-call work, so this stays outside the lock regardless.
    profile = load_scoring_profile(config)
    ctx = build_candidate_context(config, profile)

    with _CONTEXT_CACHE_LOCK:
        # Evict-oldest if we're at the cap. dict insertion order is the
        # FIFO we want; pop the first key.
        if len(_CONTEXT_CACHE) >= _CONTEXT_CACHE_MAX and key not in _CONTEXT_CACHE:
            oldest = next(iter(_CONTEXT_CACHE))
            _CONTEXT_CACHE.pop(oldest, None)
        _CONTEXT_CACHE[key] = ctx
    return ctx


def score_and_persist_job(
    job: dict,
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    config: dict,
    scorer_fn: Callable | None = None,
    *,
    run_id: str | None = None,
    timeout: float | None = None,
):
    """Unified v3.0 scoring entry point.

    - scorer_fn: defaults to job_scorer.score_job, bound to the host's wired
      call_model dispatcher (jobcannon.engine.job_scorer.score_job requires
      call_model as a required keyword-only injection -- the engine has no
      provider of its own). Injection point preserved for tests — pass your
      own reference to support mock injection.
    - timeout: optional provider-call timeout override (seconds), forwarded
      to scorer_fn ONLY when set (issue #1413's scoring-leg gap). Left
      unforwarded when None so the many existing test doubles registered via
      scorer_fn -- most are narrow lambdas of the shape
      ``lambda j, c, cfg, candidate_context, location_policy=None: ...`` with
      no **kwargs catch-all -- keep working unchanged.
    - The candidate-context block is resolved INTERNALLY via
      ``_resolve_candidate_context(config)`` — callers cannot bypass it.
      Single-point-of-enforcement: every scoring call sees the candidate's
      target locations / titles / floor / background, so the v3 rubric
      anchors (e.g. "on-site in a location candidate cannot relocate to")
      can be applied correctly. Spec D-2.1 / D-2.2.
    - Persists: classification (Python-derived), sub_scores_json,
      fit_analysis (rationale payload), scoring_provider, scoring_model,
      and the location-policy verdict JSON (echoed into fit_analysis).
    - Computes a deterministic LocationPolicy pre-LLM from the postings row
      and passes it into the scorer (prompt block) and persist
      (classification enforcement). Issue #1214.
    - Returns the underlying ScoringResult (status='ok'/'skipped'/'error')
      or None if the scorer returned nothing. Jobs missing ``dedup_key`` are
      rejected before scoring runs (returns None) — there is no row to key
      a persisted assessment against, so scoring would be wasted work.
    - run_id: optional correlation id from the scheduler / harness run
      wrapper. Accepted for call-signature compatibility with the
      ``ScanServices.score_and_persist_job`` slot; currently unused -- see
      the per-job audit event note below.

    Plan 4 Commit E removed the legacy haiku_score / sonnet_score /
    haiku_summary dual-write shim now that all readers consume
    classification + sub_scores_json + fit_analysis directly.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn  # PORT-SEAM: pooled-connection unwrap, matches _assessment_writer.py's convention

    if scorer_fn is None:
        # PORT-SEAM: private lazily imported job_finder.web.job_scorer.score_job
        # directly (its call_model was an unconditional module-level import
        # inside job_scorer, not an injected argument). The engine's ported
        # score_job requires call_model as an INJECTED keyword-only argument
        # (the engine has no provider of its own) -- bind the host's wired
        # dispatcher here via functools.partial so the default scorer_fn
        # keeps score_job's call-site shape
        # (job, conn, config, candidate_context, **kwargs).
        from jobcannon.engine import job_scorer as _job_scorer
        from jobcannon.engine.services import get_services

        _call_model = get_services().call_model
        if _call_model is None:
            raise RuntimeError(
                "score_and_persist_job: default scorer_fn requires "
                "ScanServices.call_model to be wired (see build_scan_services)"
            )
        scorer_fn = functools.partial(_job_scorer.score_job, call_model=_call_model)

    dedup_key = job.get("dedup_key")
    if not dedup_key:
        logger.warning("score_and_persist_job: missing dedup_key, skipping score+persist")
        return None

    # Pre-LLM: compute the deterministic LocationPolicy from the postings row.
    # The row is authoritative for the structured location facts; the job dict
    # passed by callers may not carry every column.
    # PORT-SEAM: `jobs` -> `postings` (flat single-writer table, L-0075);
    # `SELECT {JOBS_ALL_COLUMNS}` -> `SELECT *` narrowed to the three columns
    # actually read, matching _jobs.py's idiom of selecting only what's used.
    location_policy = None
    try:
        row = raw.execute(
            "SELECT locations_structured, workplace_type, primary_country_code "
            "FROM postings WHERE dedup_key = %s",
            (dedup_key,),
        ).fetchone()
    except Exception:
        logger.warning("score_and_persist_job: DB read failed for dedup_key=%s", dedup_key)
        row = None

    if row is not None:
        # PORT-SEAM: locations_structured is jsonb -- psycopg auto-decodes to
        # a Python list/None, no json.loads needed (unlike private's sqlite3
        # TEXT column). PORT-SEAM: no public `postings` JSON sub-postings
        # column exists (L-0075's flat single-writer model subsumes the
        # per-posting collection) -- pass postings=None. PORT-SEAM: no public
        # has_subcountry_constraint column exists -- always False, so the
        # #1202 sub-country gate never fires on this host (BEHAVIOR CHANGE,
        # not a mechanical seam; called out in the PR body's fidelity items).
        location_policy = compute_location_policy(
            locations_structured=row["locations_structured"] or [],
            workplace_type=row["workplace_type"],
            primary_country_code=row["primary_country_code"],
            postings=None,
            config=config,
            has_subcountry_constraint=False,
        )

    candidate_context = _resolve_candidate_context(config)
    scorer_kwargs = {
        "candidate_context": candidate_context,
        "location_policy": location_policy,
    }
    if timeout is not None:
        scorer_kwargs["timeout"] = timeout
    result = scorer_fn(
        job,
        conn,
        config,
        **scorer_kwargs,
    )

    if result is None:
        logger.info("score_and_persist_job: no result for dedup_key=%s", dedup_key)
        return None

    # Pass-through for skipped / error envelopes — no DB write, no raise.
    if getattr(result, "status", None) != "ok" or result.data is None:
        logger.info(
            "score_and_persist_job: skip dedup_key=%s status=%s error=%s",
            dedup_key,
            getattr(result, "status", None),
            getattr(result, "error", None),
        )
        return result

    assessment = result.data
    provider = result.provider
    model = getattr(result, "model", None)

    # PORT-SEAM: private's per-posting set_postings(...) write dropped here
    # -- no public `jobs.postings` JSON column exists; the flat `postings`
    # table (L-0075) already carries one row per job, so there is no
    # sub-postings collection for apply_location_policy_to_postings to
    # annotate. `location_policy=lp` -> `location_policy_verdict_json=
    # verdict_to_json(lp)` below is _assessment_writer's own string seam.
    location_policy_verdict_json = (
        verdict_to_json(location_policy) if location_policy is not None else None
    )

    classification = persist_job_assessment(
        conn,
        dedup_key,
        assessment,
        provider=provider,
        model=model,
        config=config,
        location_policy_verdict_json=location_policy_verdict_json,
    )
    # PORT-SEAM: private's trailing conn.commit() dropped here --
    # persist_job_assessment already calls pool.commit_unless_nested(raw)
    # internally (see _assessment_writer.py's module docstring: "does not
    # commit on its own beyond pool.commit_unless_nested"); a second commit
    # here would be redundant at best and would raise inside a nested
    # `with conn.transaction():` (tests/host/conftest.py's ambient pattern).

    # PORT-SEAM: private's per-job run_events.mark(...) audit event dropped
    # here -- no public run_events table exists (design note Q-D: "drop for
    # the port; run-envelope log_run already covers auditing"). Revisit only
    # if a future dashboard or test needs per-job granularity.

    return result


def _render_location_targeting(
    work_arrangement: str | None, target_locations: list[str]
) -> list[str]:
    """Render work-arrangement + geographies as a PREFERENCE HIERARCHY.

    The candidate's ``work_arrangement`` is their *preferred* arrangement (a
    ranking, not a hard filter), and the geographic ``target_locations`` are the
    places where a *non-remote* role is acceptable. Two rules keep this honest:

    - The ``"remote"`` token is stripped from the geography list. "Remote" is a
      modality, not a place; it is already carried by ``work_arrangement``. This
      single-sources the treatment with ``location_fit._target_loc_matches``,
      which likewise excludes ``"remote"`` from geography matching.
    - When remote is preferred, a fully-remote role is stated as the IDEAL
      location match, AND an on-site/hybrid role in a target geography is stated
      as an *equally* full location match (location_fit 5) — both are explicitly
      disqualified from being a location "gap". Remote is the candidate's top
      *overall* preference, but on the location_fit axis a target-geography
      on-site/hybrid role scores the same as remote (5): geography membership
      overrides the on-site penalty.

    This exists because the prior flat rendering ("Target locations: San
    Francisco, Remote") gave the scorer no ordering, so ``qwen2.5:14b`` read the
    first-listed geography as the candidate's preference and inverted it —
    flagging a fully-remote role as the gap "Remote role, candidate prefers San
    Francisco location". Making the hierarchy explicit removes the ambiguity the
    model was resolving incorrectly.

    The remote branch deliberately does NOT tell the model a remote role is
    "preferred over" a target-geography on-site/hybrid role *for location_fit*.
    That earlier wording applied downward pressure to the location_fit sub-score
    of an in-geography hybrid role in the LLM-judged branch (when the
    deterministic override abstains — e.g. a job whose city failed to parse into
    ``locations_structured``), contradicting the override's own Row 5 (which
    scores a target-geography on-site/hybrid role a 5). Keeping the two branches'
    location_fit guidance symmetric single-sources the axis with the override.

    Both branches also state the negative case explicitly: a role outside every
    listed geography (on-site/hybrid, or with no stated policy at all) is
    1-2, not the base rubric's flat "hybrid = 3" anchor. Without this, the
    LLM-judged branch (override abstains — e.g. ``workplace_type`` is
    UNSPECIFIED because the source ATS/JD never states a policy, per
    ``location_fit.compute_location_fit``'s Row R-b/4 gaps) falls back to that
    generic anchor regardless of whether the commute is actually feasible —
    e.g. a Boston-hybrid posting scored a flat 3 for a candidate targeting
    only San Francisco / remote, the same score a truly-commutable local
    hybrid role would get (CarGurus Principal Data Analyst, 2026-07-11).
    """
    wa = (work_arrangement or "remote").strip().lower()
    # Keep only real places: drop blanks/None AND the "remote" modality token.
    # This mirrors location_fit._target_loc_matches, which _norm-coerces every
    # entry (None → "") and skips falsy tokens — so a bare "- " list item in a
    # hand-edited config (PyYAML yields None) or an eval-harness config renders
    # cleanly instead of crashing the ", ".join below.
    geos = [
        t.strip()
        for t in target_locations
        if (t or "").strip() and (t or "").strip().lower() != "remote"
    ]
    geo_str = ", ".join(geos) if geos else "Not specified"

    lines: list[str] = [f"- Preferred work arrangement: {wa}"]
    if wa == "remote":
        lines.append(
            "- A fully remote role is the candidate's IDEAL location match "
            "(location_fit 5). Remote is the top preference, NOT a shortcoming — "
            "never list a remote role as a location gap."
        )
        lines.append(
            f"- Acceptable on-site/hybrid geographies (relevant ONLY when the role "
            f"is not remote): {geo_str}. An on-site or hybrid role in one of these "
            "is an EQUALLY full location match (location_fit 5) — geography "
            "membership overrides the on-site penalty, so score it the same as a "
            "remote role on location_fit and never treat its on-site/hybrid "
            "arrangement as a location gap. (A remote role remains the top overall "
            "preference, but that ranking must not lower this on-site/hybrid "
            "role's location_fit.)"
        )
        lines.append(
            "- A role that is on-site/hybrid (or states no clear remote/hybrid/"
            "on-site policy) OUTSIDE every geography listed above is NOT a "
            "'feasible partial-commute' — it requires full relocation for this "
            "candidate. Score it 1-2 on location_fit, never the generic 3 the "
            "base rubric anchors to plain 'hybrid': that anchor assumes the "
            "commute is actually feasible, which does not hold once the role "
            "sits outside every acceptable geography."
        )
    else:
        lines.append(
            f"- Target geographies: {geo_str}. On-site/hybrid in one of these is a "
            "match — geography membership overrides the on-site penalty for "
            "location_fit. A fully remote role is also a strong match."
        )
        lines.append(
            "- A role that is on-site/hybrid (or states no clear remote/hybrid/"
            "on-site policy) OUTSIDE every geography listed above requires full "
            "relocation for this candidate. Score it 1-2 on location_fit, never "
            "the generic 3 the base rubric anchors to plain 'hybrid': that "
            "anchor assumes the commute is actually feasible, which does not "
            "hold once the role sits outside every target geography."
        )
    # Pointer to the per-job LocationPolicy verdict (issue #1214), kept to one
    # short line so build_candidate_context stays within its ~2400-char/~600-
    # token budget (test_candidate_context.py::test_token_budget_under_600).
    # An earlier draft spelled out a hardcoded "Remote (5) > Hybrid SF (4) > ..."
    # ranking here, but that both blew the budget AND contradicted the
    # branch-specific rules above (which score an on-site/hybrid role in a
    # target geography a 5, not a 4/3/2/1) — the per-job policy analysis in
    # the user message is the authoritative source for the actual verdict.
    lines.append("- Per-job location-policy analysis (rank + eligibility) in user message.")
    return lines


def build_candidate_context(config: dict, profile: dict) -> str:
    """Merge config.yaml [profile] (targeting) and experience_profile.json
    (resume) into a prompt-ready candidate-context string.

    Returns a structured-text block ~400-500 tokens that gets spliced into
    the scoring system prompt between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES
    per spec D-2.1. Output stays under ~600 tokens (~2400 chars) via top-30
    skills + first-6 positions truncation.

    Args:
        config: Application config dict. Reads ``config["profile"]`` for
            targeting fields (target_titles, target_locations, min_salary,
            industries, exclusions).
        profile: Experience profile dict (typically loaded via
            load_scoring_profile). Reads positions, skills, education.

    Returns:
        A multi-section markdown string with "## Candidate context" header.
        Always returns a non-empty string even when both inputs are empty
        (uses "Not specified" / "No positions" sentinels).
    """
    cfg_profile = config.get("profile") or {}

    # Targeting block
    target_titles = cfg_profile.get("target_titles") or []
    target_locations = cfg_profile.get("target_locations") or []
    work_arrangement = cfg_profile.get("work_arrangement") or "remote"
    min_salary = cfg_profile.get("min_salary")
    industries = cfg_profile.get("industries") or []
    exclusions = cfg_profile.get("exclusions") or {}
    excl_companies = exclusions.get("companies") or []

    parts: list[str] = ["## Candidate context", "", "### Targeting"]
    parts.append(
        f"- Target titles: {', '.join(target_titles) if target_titles else 'Not specified'}"
    )
    if target_titles:
        parts.append(
            "  (These are exemplars of the candidate's role-function intent, not an "
            "exhaustive whitelist. Near-variants — same role function with adjacent "
            "wording, e.g. 'Lead Data Analyst' for 'Lead Analyst', or 'Senior/Staff "
            "Data Scientist' for 'Senior Data Scientist' — count as title matches "
            "and should score title_fit >= 4. Score 5 only for exact-or-stronger matches.)"
        )
    parts += _render_location_targeting(work_arrangement, target_locations)
    parts.append(
        f"- Compensation floor: ${min_salary:,}"
        if min_salary
        else "- Compensation floor: Not specified"
    )
    parts.append(
        f"- Target industries: {', '.join(industries) if industries else 'Not specified'}"
    )
    if excl_companies:
        parts.append(f"- Exclusions: companies {excl_companies}")

    # Resume block
    parts += ["", "### Background"]
    positions = profile.get("positions") or []
    if not positions:
        parts.append("- No positions in profile")
    else:
        for p in positions[:6]:  # cap at 6 most recent
            title = p.get("title", "?")
            company = p.get("company", "?")
            start = p.get("start_date", "?")
            end = p.get("end_date") or "present"
            parts.append(f"- {title} @ {company} ({start}-{end})")

    skills = profile.get("skills") or []
    if skills:
        parts.append(f"- Top skills: {', '.join(skills[:30])}")

    education = profile.get("education") or []
    for e in education[:3]:
        deg = e.get("degree") or "?"
        inst = e.get("institution") or "?"
        grad = e.get("graduation") or ""
        parts.append(f"- {deg} ({inst}{', ' + str(grad) if grad else ''})")

    return "\n".join(parts)
