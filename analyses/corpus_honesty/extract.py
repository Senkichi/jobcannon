"""Classify every posting into exactly one provenance class and pull its
staleness/expiry/enrichment outcome fields.

Provenance classes (precedence order — a posting confirmed at more than one
level is classified at the HIGHEST-trust level it reached, e.g. a posting
seen on both an aggregator feed and a direct ATS scan is 'ats_confirmed'):

  1. ats_confirmed    - at least one source is a direct ATS-scanner sighting
                         (the scanner hit the platform's own API/board).
  2. direct_crawl     - at least one source is the project's own first-party
                         careers-page crawler (NOT a third-party aggregator).
  3. aggregator_only   - at least one source is a third-party aggregator/portal
                         or search-engine-mediated listing.
  4. email_alert_only  - at least one source is an inbox job-alert digest
                         (LinkedIn, Glassdoor, Indeed, ZipRecruiter, Monster,
                         TrueUp, JobRight) and none of the above applied.
  5. unattributed      - empty `sources`, or every entry is an internal
                         bookkeeping tag that is not a discovery channel at
                         all (a manually-added row, an off-platform-email
                         pipeline signal). Excluded from the primary result.
  6. unrecognized      - a source string that matches none of classes 1-4
                         above - a drift guard, not a real class. Excluded
                         from the primary result (see the label-set comment
                         below: this repo cannot import the private
                         pipeline's code, so the taxonomy here is a verified
                         point-in-time snapshot, not a live import).

This is a corrected, registry-derived version of an ad hoc classification
used in the scoping exploration (explore2.py's `is_ats_like`/`is_portal_like`):
that script bucketed the project's own first-party crawler ('careers_crawl',
'careers_page') and an email-digest sender ('jobright') as "portal-like"
aggregator sources, and fell three purely-internal bookkeeping rows
('manual', 'off_platform_email') into "email_alert_only" by an unguarded
`else` branch. Both are fixed here.
"""

from __future__ import annotations

import json
from sqlite3 import Connection

import pandas as pd

# ---------------------------------------------------------------------------
# Provenance label sets.
#
# This analysis repo is deliberately host-agnostic (see tests/engine's import
# guard) and carries no dependency on the private `job_finder` pipeline, so
# these sets cannot be a live import - they are a verified snapshot of that
# pipeline's own source-tag vocabulary, captured 2026-07-16 against the live
# corpus. Provenance for each set (private-repo paths, for anyone auditing
# this snapshot against the pipeline later):
#
#   ATS_CONFIRMED_LABELS: every PlatformScanner.company_source /
#     PlaywrightPlatformScanner.company_source string in
#     job_finder/web/ats_platforms/_platforms_*.py, unioned via
#     job_finder.web.ats_platforms.SCANNERS_BY_NAME + .PLAYWRIGHT_SCANNERS.
#     This is the set of display-cased labels a *direct* ATS-scanner sighting
#     writes into jobs.sources - distinct in case from the lowercase labels
#     an email alert about the same platform would write (see EMAIL_ALERT
#     'greenhouse' below), which is the load-bearing signal that keeps the
#     two channels from colliding.
#   EMAIL_ALERT_LABELS: every SenderSpec.label in
#     job_finder/sources/email_senders.py's SENDERS registry (one row per
#     inbox alert sender the IMAP pipeline parses).
#   DIRECT_CRAWL_LABELS: the two source tags the project's own careers-page
#     crawler writes - job_finder/web/careers_crawler/_persistence.py
#     ("careers_crawl") and job_finder/web/ats_scanner/_run_html.py
#     ("careers_page"). No registry constant exports these; they are a
#     first-party (not third-party-aggregator) discovery channel and get
#     their own class per a robustness caveat raised during scoping.
#   AGGREGATOR_PREFIX: every job_finder/sources/portal_search_source.py
#     portal fetcher (_PORTAL_FETCHERS) writes its source tag as
#     f"portal_{name}" - a structural convention, not an enumerated list, so
#     a new portal fetcher is picked up automatically.
#   AGGREGATOR_EXTRA_LABELS: search-engine-mediated aggregator sources with
#     no "portal_" prefix (job_finder/sources/dataforseo_source.py,
#     serpapi_source.py, google_cse_source.py), plus "thordata" - a scraping
#     source whose module was deleted from the private pipeline (see that
#     repo's project memory: "Thordata DELETED"); the tag survives only on
#     legacy rows ingested before removal and is kept here so those rows
#     still classify correctly instead of falling to 'unrecognized'.
#   META_LABELS: internal bookkeeping tags that are not a discovery channel -
#     "manual" (job_finder/web/blueprints/jobs.py, job_finder/db/_persistence.py
#     default), "off_platform_email" (job_finder/web/pipeline_detector/
#     _off_platform.py - an off-platform APPLICATION signal, not a listing
#     sighting), "primary_source_llm" (job_finder/web/primary_source_merge.py -
#     a tie-breaker tag that always rides alongside a real source tag, never
#     appears alone in the live corpus).
#
# If the private pipeline adds a new source tag without a matching update
# here, it lands in 'unrecognized' (reported, excluded from the primary
# result) rather than being silently absorbed into the wrong bucket.
# ---------------------------------------------------------------------------

ATS_CONFIRMED_LABELS: frozenset[str] = frozenset(
    {
        "ADP",
        "Amazon",
        "Ashby",
        "BambooHR",
        "Breezy",
        "Eightfold",
        "Google",
        "Greenhouse",
        "IBM",
        "JazzHR",
        "Jobvite",
        "Lever",
        "Microsoft Careers",
        "Oracle Cloud",
        "Paylocity",
        "Personio",
        "Phenom",
        "Pinpoint",
        "Recruitee",
        "Rippling",
        "SmartRecruiters",
        "SuccessFactors",
        "Teamtailor",
        "Tesla",
        "UltiPro",
        "Workable",
        "Workday",
        "iCIMS",
    }
)

EMAIL_ALERT_LABELS: frozenset[str] = frozenset(
    {
        "glassdoor",
        "greenhouse",  # inbox alert about a greenhouse-hosted job - lowercase,
        # never collides with the ATS scanner's "Greenhouse" (see module docstring)
        "indeed",
        "indeed_match",
        "jobright",
        "linkedin",
        "monster",
        "trueup",
        "ziprecruiter",
    }
)

DIRECT_CRAWL_LABELS: frozenset[str] = frozenset({"careers_crawl", "careers_page"})

AGGREGATOR_PREFIX = "portal_"
AGGREGATOR_EXTRA_LABELS: frozenset[str] = frozenset(
    {"dataforseo", "serpapi", "google_cse", "thordata"}
)

META_LABELS: frozenset[str] = frozenset({"manual", "off_platform_email", "primary_source_llm"})

EXCLUDED_BUCKETS: frozenset[str] = frozenset({"unattributed", "unrecognized"})

PROVENANCE_SQL = """
SELECT sources, is_stale, expiry_status, jd_full, sub_scores_json
FROM jobs
"""

EXCLUSION_SQLS = {
    "total": "SELECT COUNT(*) FROM jobs",
    "malformed_sources_json": (
        "SELECT COUNT(*) FROM jobs WHERE sources IS NULL OR json_valid(sources) = 0"
    ),
}


def _is_aggregator_tag(source: str) -> bool:
    return source.startswith(AGGREGATOR_PREFIX) or source in AGGREGATOR_EXTRA_LABELS


def _parse_sources(raw: str | None) -> list[str] | None:
    """Parse a `sources` JSON-array cell; None (not []) signals unparseable.

    A malformed cell is counted separately by `malformed_sources_json` in
    load_exclusion_counts - callers must skip a None result rather than treat
    it as an empty/unattributed source list, or a data-quality row would
    silently double as a real "no source" observation.
    """
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def classify_source(sources: list[str], *, exclude: str | None = None) -> str:
    """Classify one posting's `sources` array into exactly one provenance bucket.

    `exclude`, when given, removes that single source tag from consideration
    before classifying - the mechanism behind the "drop the largest single
    aggregator source" robustness variant (see transform.py).
    """
    real = [s for s in sources if s not in META_LABELS and s != exclude]
    if not real:
        return "unattributed"
    if any(s in ATS_CONFIRMED_LABELS for s in real):
        return "ats_confirmed"
    if any(s in DIRECT_CRAWL_LABELS for s in real):
        return "direct_crawl"
    if any(_is_aggregator_tag(s) for s in real):
        return "aggregator_only"
    if any(s in EMAIL_ALERT_LABELS for s in real):
        return "email_alert_only"
    return "unrecognized"


def load_provenance_records(con: Connection, *, exclude_source: str | None = None) -> pd.DataFrame:
    """One row per posting with a real provenance class + its outcome fields.

    Postings that classify as 'unattributed' or 'unrecognized' are dropped
    here (see EXCLUDED_BUCKETS) - counted in load_exclusion_counts instead,
    mirroring posting_lifespan's exclusion-before-estimate discipline.
    """
    rows = con.execute(PROVENANCE_SQL).fetchall()
    records = []
    for sources_json, is_stale, expiry_status, jd_full, sub_scores_json in rows:
        sources = _parse_sources(sources_json)
        if sources is None:
            continue  # malformed JSON - counted by malformed_sources_json instead
        bucket = classify_source(sources, exclude=exclude_source)
        if bucket in EXCLUDED_BUCKETS:
            continue
        records.append(
            {
                "bucket": bucket,
                "is_stale": bool(is_stale),
                "expiry_status": expiry_status if expiry_status is not None else "null",
                "has_jd": bool(jd_full),
                "is_scored": sub_scores_json is not None,
            }
        )
    return pd.DataFrame(
        records, columns=["bucket", "is_stale", "expiry_status", "has_jd", "is_scored"]
    )


def load_exclusion_counts(con: Connection) -> dict[str, int]:
    counts = {name: con.execute(sql).fetchone()[0] for name, sql in EXCLUSION_SQLS.items()}
    rows = con.execute(PROVENANCE_SQL).fetchall()
    bucket_counts: dict[str, int] = {}
    for sources_json, *_rest in rows:
        sources = _parse_sources(sources_json)
        if sources is None:
            continue  # malformed JSON - counted by malformed_sources_json instead
        bucket = classify_source(sources)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    counts["no_attributable_source"] = bucket_counts.get("unattributed", 0)
    counts["unrecognized_source_tag"] = bucket_counts.get("unrecognized", 0)
    counts["usable"] = (
        counts["total"]
        - counts["malformed_sources_json"]
        - counts["no_attributable_source"]
        - counts["unrecognized_source_tag"]
    )
    return counts
