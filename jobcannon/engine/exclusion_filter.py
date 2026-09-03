# PORTED from job_finder/web/exclusion_filter.py @ 099531b7ede7c2a86c148b00c35d7dabf0a879d1 (private job-cannon). Ledger L-0179.
"""Pre-scoring exclusion filter. Zero API cost -- pure string matching.

Provides should_exclude() to determine whether a job should be skipped
before any scoring call, based on title keywords, excluded companies,
and a configurable salary floor.
"""

import logging

# PORT-SEAM: relocated from job_finder.config (DIES) — a hardcoded default
# denylist plus a pure (config) -> frozenset merge helper, not a live config
# fetch (config is already an injected param at this module's one call site,
# should_exclude(job, config=...)). Generic aggregator/scam-name noise, not
# owner-specific data, so it carries verbatim.
from jobcannon.engine.normalizers import normalize_company
from jobcannon.engine.job_scorer import scoring_precheck
from jobcannon.engine.source_registry import is_unverifiable_candidate

logger = logging.getLogger(__name__)

# PORT-SEAM: relocated from job_finder.config (DIES) — a hardcoded default
# denylist plus a pure (config) -> frozenset merge helper, not a live config
# fetch (config is already an injected param at this module's one call site,
# should_exclude(job, config=...)). Generic aggregator/scam-name noise, not
# owner-specific data, so it carries verbatim.
_RAW_COMPANY_DENYLIST: frozenset[str] = frozenset(
    {
        # Placeholder / scam names
        "unknown",
        "medical jobs",
        "clinical jobs",
        "remotehunter",
        "jobgether",
        "mercor",
        "crossing hurdles",
        # Aggregators / re-posters
        "virtual vocations",
        "prosidian consulting",
        "synergisticit",
        "synergistic it",
        # role.com: AI job-search aggregator that republishes OTHER employers'
        # postings under one bogus company record. See BLOCKED_DOMAINS
        # (domain_policy.py) for the companion host-level block.
        "role",
    }
)


def _normalize_denylist(entries) -> frozenset[str]:
    """Normalize denylist entries via normalize_company for suffix-variant parity."""
    return frozenset(normalize_company(e) for e in entries if e and normalize_company(e))


# Normalized form used by all matching sites. Built once at import.
COMPANY_DENYLIST: frozenset[str] = _normalize_denylist(_RAW_COMPANY_DENYLIST)


def get_company_denylist(config: dict) -> frozenset[str]:
    """Return the company denylist, merging config entries with hardcoded defaults.

    Config entries are additive — the hardcoded defaults are always included.
    Both the hardcoded seed and config entries are normalized via
    normalize_company so the returned set matches the same canonical form the
    matching sites (should_exclude) compute from the stored brand.
    """
    config_entries = (config.get("filters", {}) or {}).get("company_denylist", [])
    extra = _normalize_denylist(config_entries)
    return COMPANY_DENYLIST | extra


def clears_salary_floor(salary_max, salary_currency, min_salary) -> bool:
    """True when salary_max clears the configured floor (>= 85% of min_salary).

    Single source of truth for the salary-floor arithmetic. should_exclude()
    uses this to decide whether to exclude a NEW/unscored job; the T4.2/D20
    re-evaluation hooks (job_finder.db._jobs.upsert_job and
    data_enricher._persist) use the same predicate to decide whether a
    reconciled salary correction should clear an EXISTING salary_floor
    dismissal. One arithmetic definition, two call sites, so they cannot
    drift the way count_scorable's SQL/Python copies once did (see the
    module docstring above).

    Mirrors should_exclude's disclosure gates: undisclosed/non-numeric/
    non-positive/non-USD salaries never clear the floor (treated the same
    as "no signal", not as "clears").
    """
    if min_salary is None or salary_max is None:
        return False
    if not isinstance(salary_max, (int, float)) or salary_max <= 0:
        return False
    if (salary_currency or "USD") != "USD":
        return False
    return salary_max >= min_salary * 0.85


# ── Single source of truth for "which unscored jobs are scorable" ────────────
#
# The dashboard "Score N unscored jobs" tile, the batch-session ``total``, and
# the batch worker's per-row decision MUST agree on the same set, or the button
# advertises jobs the worker silently no-ops and its count never decrements after
# a click (the recurring Score-Now desync — bitten on the jd_full gate, then the
# P3.2 location gate, then again every time the two implementations drift).
#
# Earlier fixes kept a SECOND, SQL re-implementation of the scoring gates inside
# count_scorable and pinned it to the Python source with a parity test. That is
# inherently fragile: SQL and Python disagree on JSON / NULL / whitespace edge
# cases the fixtures never exercise (e.g. ``locations_structured = 'null'`` or
# ``'[ ]'`` — not literally ``''``/``'[]'`` so SQL calls them "location-ready",
# but Python parses them to an empty list and gates them), and every new gate has
# to be mirrored in two languages in lockstep. So the SQL copy is gone. There is
# now ONE definition of "scorable":
#
#   * SCORABLE_CANDIDATE_WHERE — the cheap, index-friendly SQL pre-filter that
#     selects the UNSCORED candidate universe. Shared verbatim by count_scorable
#     AND the batch worker's SELECT, so they cannot disagree on the universe.
#   * is_scorable(job, config)  — the pure Python predicate (should_exclude +
#     scoring_precheck) the worker applies per row. count_scorable counts the
#     candidates that pass it; the worker scores them. Same functions, no drift.
#
# Any future scoring gate added to scoring_precheck is reflected in the count
# automatically — there is no SQL translation left to forget to update.

# Cheap pre-filter: the unscored candidate universe (classification IS NULL),
# minus dismissed/archived and quarantined (I-16/I-17) rows. This is the SQL the
# worker SELECTs; count_scorable SELECTs the same set. Per-row scorability is then
# decided in Python by is_scorable() — NOT in SQL — so the two never diverge.
#
# Wrapped in parens: at least one caller (backfill_enrichment.py) interpolates
# this constant into a larger WHERE clause alongside its own AND-ed conditions
# (e.g. "jd_full IS NOT NULL AND " + SCORABLE_CANDIDATE_WHERE). Without the
# parens, a future edit that adds a top-level OR to this constant would
# misbind across that composition (`a AND x OR y` instead of `a AND (x OR y)`)
# and could select rows the composing caller's own AND was meant to exclude.
# The parens make the constant safe to interpolate into ANY larger WHERE
# clause regardless of what boolean structure it grows internally.
SCORABLE_CANDIDATE_WHERE = (
    "(classification IS NULL "
    "AND pipeline_status NOT IN ('dismissed', 'archived') "
    "AND COALESCE(unresolved_reasons, '[]') = '[]')"
)

# Freshest-first scoring queue; served by idx_jobs_last_seen. Used by the worker
# (count_scorable does not care about order).
SCORABLE_CANDIDATE_ORDER_BY = "ORDER BY last_seen DESC"


def should_exclude(
    job_row: dict,
    exclusions: dict,
    min_salary: int | None = None,
    config: dict | None = None,
) -> tuple[bool, str, str]:
    """Check if a job should be excluded before scoring.

    Args:
        job_row: Job record dict with at minimum: title (str), company (str),
                 salary_max (int|None), salary_currency (str|None).
        exclusions: Dict with optional keys:
                    - title_keywords (list[str]): Substrings to match against job title.
                    - companies (list[str]): Company names to exclude.
        min_salary: Candidate's minimum acceptable salary. If provided, salary_max is
                    disclosed, salary_currency is 'USD', and salary_max < min_salary * 0.85,
                    the job is excluded. Pass None to skip salary floor check.
        config: Optional full config dict. If provided, merges config.yaml
                filters.company_denylist entries with hardcoded defaults.
                If None, only the hardcoded COMPANY_DENYLIST is used.

    Returns:
        (True, rule_tag, detailed_text) if the job should be excluded, (False, "", "") otherwise.
        rule_tag is a coarse category (title_kw|company|salary_floor) for GROUP BY aggregation.
        detailed_text is the human-readable evidence for pipeline_events.evidence.
    """
    title = job_row.get("title", "") or ""
    company = job_row.get("company", "") or ""
    salary_max = job_row.get("salary_max")

    title_lower = title.lower()
    # Normalize the stored brand the same way the denylist is normalized, so
    # legal-entity-suffix variants match (#213): "Virtual Vocations Inc" and a
    # denylist entry of "Virtual Vocations" both reduce to "virtual vocations".
    company_normalized = normalize_company(company)

    # 1. Title keyword exclusions (case-insensitive substring match)
    for keyword in exclusions.get("title_keywords", []):
        if not keyword:
            continue
        if keyword.lower() in title_lower:
            return True, "title_kw", f"Title contains excluded keyword: '{keyword}'"

    # 2. Company exclusions (config + denylist), compared on normalize_company so
    #    suffix variants ("Acme, Inc." == "Acme") and aggregator re-posters fire.
    #    User-supplied exclusions.companies are normalized to the same form.
    excluded_companies = {normalize_company(c) for c in exclusions.get("companies", []) if c}
    # Merge in the denylist (hardcoded defaults + optional config entries, already normalized)
    denylist = get_company_denylist(config) if config else COMPANY_DENYLIST
    excluded_companies_set = excluded_companies | denylist
    if company_normalized and company_normalized in excluded_companies_set:
        return True, "company", f"Excluded company: '{company.strip()}'"

    # 3. Salary floor check (only when min_salary provided and salary_max disclosed)
    if (
        min_salary is not None
        and salary_max is not None
        and isinstance(salary_max, (int, float))
        and salary_max > 0
        and (job_row.get("salary_currency") or "USD") == "USD"
        and not clears_salary_floor(salary_max, job_row.get("salary_currency"), min_salary)
    ):
        return True, "salary_floor", f"Max salary ${salary_max:,} below floor ${min_salary:,}"

    return False, "", ""


# Lightweight projection for counting: the columns is_scorable's gates read.
# jd_full is projected as a presence sentinel ('' / non-empty), NOT its real
# text — scoring_precheck's D5 jd-content gate ("awaiting_jd_adjudication")
# reads a PERSISTED verdict column (jd_content_verdict, stamped once at the
# set_jd_full write chokepoint) rather than recomputing classify_jd_content
# per row, so the real body is never needed on this counting path. See
# set_jd_full's docstring (job_finder/db/_jd_full.py) for the write-time
# stamping and scoring_precheck's docstring (job_finder/web/job_scorer.py)
# for the fail-open-on-NULL semantics that keep this projection correct even
# for rows a verdict hasn't been computed for yet.
# sources, source_urls, and direct_url are required for is_unverifiable_candidate.
_SCORABLE_COLS = (
    "title, company, salary_max, salary_currency, "
    "locations_structured, location, enrichment_tier, unresolved_reasons, "
    "sources, source_urls, direct_url, "
    "CASE WHEN TRIM(COALESCE(jd_full, '')) <> '' THEN 'x' ELSE '' END AS jd_full, "
    "jd_adjudicated_version, jd_content_verdict"
)


def is_scorable(job: dict, config: dict) -> bool:
    """Pure predicate: would the batch worker actually SCORE this row?

    THE single source of truth for "scorable", shared by ``count_scorable`` (the
    dashboard tile + batch ``total``) and the batch worker. A candidate row is
    scorable iff it is not excluded (``should_exclude``) and passes every
    completeness gate (``scoring_precheck`` returns ``None``) — the exact
    checks the worker applies per row, in the same order. Because the count and
    the worker call the SAME functions, the tile can never advertise a job the
    worker silently no-ops.

    Pure: no I/O, no mutation. (The worker layers its exclusion auto-dismiss
    side effect on top separately; counting must never mutate.) Callers pass
    rows from SCORABLE_CANDIDATE_WHERE, so classification / pipeline_status /
    quarantine are already filtered in SQL; this adds the per-row exclusion +
    completeness gates that are impractical to express faithfully in SQL.
    """
    exclusions = config.get("profile", {}).get("exclusions", {})
    min_salary = config.get("profile", {}).get("min_salary")
    if should_exclude(job, exclusions, min_salary, config=config)[0]:
        return False
    if is_unverifiable_candidate(job, config):
        return False
    return scoring_precheck(job) is None


def count_scorable(conn, config: dict) -> int:
    """Count unscored jobs the batch worker would actually score.

    Single-source design: SELECT the coarse candidate universe via the shared
    ``SCORABLE_CANDIDATE_WHERE`` (identical to the worker's SELECT), then count
    the rows that pass ``is_scorable`` — the SAME pure Python predicate
    (``should_exclude`` + ``scoring_precheck``) the worker applies per row.

    This replaces a prior SQL re-implementation of the scoring gates. A parallel
    SQL translation of ``scoring_precheck`` drifted from the Python source every
    time a gate was added or the data hit an untested JSON/NULL edge case (e.g.
    ``locations_structured = 'null'`` or ``'[ ]'``), producing the recurring
    "Score N unscored" tile that counts rows the worker no-ops and never
    decrements. Deriving the count from the worker's own predicate makes that
    desync structurally impossible. The candidate universe is the UNSCORED set
    (``classification IS NULL``), which is inherently small, so the per-row
    Python pass is cheap.

    Returns 0 (and logs a WARNING with traceback) on any DB error — a dashboard
    render must never 500 because the count query failed.
    """
    try:
        cur = conn.execute(f"SELECT {_SCORABLE_COLS} FROM jobs WHERE {SCORABLE_CANDIDATE_WHERE}")
        cols = [d[0] for d in cur.description]
        return sum(1 for row in cur if is_scorable(dict(zip(cols, row, strict=True)), config))
    except Exception:
        logger.warning("count_scorable failed; returning 0", exc_info=True)
        return 0
