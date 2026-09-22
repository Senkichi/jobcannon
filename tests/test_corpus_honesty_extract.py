from analyses.common.db import open_readonly
from analyses.corpus_honesty.extract import (
    _LEGACY_ATS_LABELS,
    _SCANNER_ATS_LABELS,
    ATS_CONFIRMED_LABELS,
    classify_source,
    load_exclusion_counts,
    load_provenance_records,
)
from jobcannon.engine.ats_platforms import PLAYWRIGHT_SCANNERS, SCANNERS_BY_NAME
from tests.fixtures import build_fixture_db

COMPANIES = [{"id": 1, "name": "A", "ats_platform": "greenhouse"}]


# ---------------------------------------------------------------------------
# classify_source - unit tests over the taxonomy precedence rules.
# ---------------------------------------------------------------------------


def test_ats_confirmed_direct_scan():
    assert classify_source(["Greenhouse"]) == "ats_confirmed"
    assert classify_source(["Workday"]) == "ats_confirmed"


def test_direct_crawl_first_party():
    assert classify_source(["careers_crawl"]) == "direct_crawl"
    assert classify_source(["careers_page"]) == "direct_crawl"


def test_aggregator_only_portal_prefix_and_named_extras():
    assert classify_source(["portal_jooble"]) == "aggregator_only"
    assert classify_source(["dataforseo"]) == "aggregator_only"
    assert classify_source(["serpapi"]) == "aggregator_only"
    assert classify_source(["thordata"]) == "aggregator_only"  # legacy, module removed


def test_email_alert_only():
    assert classify_source(["linkedin"]) == "email_alert_only"
    assert classify_source(["glassdoor"]) == "email_alert_only"


def test_jobright_is_email_alert_not_aggregator():
    # Corrects the scoping exploration's ad hoc classifier, which bucketed
    # jobright (an inbox job-match digest) as "portal-like".
    assert classify_source(["jobright"]) == "email_alert_only"


def test_case_sensitivity_separates_ats_scan_from_email_alert():
    # "Greenhouse" (display-cased) = a direct ATS-scanner sighting.
    # "greenhouse" (lowercase) = an inbox alert about a greenhouse-hosted job.
    # These are different provenance channels and must not collide.
    assert classify_source(["Greenhouse"]) == "ats_confirmed"
    assert classify_source(["greenhouse"]) == "email_alert_only"


def test_posting_seen_via_both_aggregator_and_ats_classifies_ats_confirmed():
    assert classify_source(["portal_jooble", "Greenhouse"]) == "ats_confirmed"
    assert classify_source(["Greenhouse", "portal_jooble"]) == "ats_confirmed"


def test_meta_bookkeeping_tags_alone_are_unattributed():
    assert classify_source([]) == "unattributed"
    assert classify_source(["manual"]) == "unattributed"
    assert classify_source(["off_platform_email"]) == "unattributed"


def test_meta_tag_rides_along_a_real_source_without_changing_bucket():
    assert classify_source(["Greenhouse", "primary_source_llm"]) == "ats_confirmed"


def test_unrecognized_source_tag():
    assert classify_source(["some_future_source_nobody_has_seen_yet"]) == "unrecognized"


def test_exclude_param_drops_one_tag_before_classifying():
    assert classify_source(["portal_jooble"], exclude="portal_jooble") == "unattributed"
    assert (
        classify_source(["portal_jooble", "linkedin"], exclude="portal_jooble")
        == "email_alert_only"
    )
    # Excluding a tag that isn't present is a no-op.
    assert classify_source(["Greenhouse"], exclude="portal_jooble") == "ats_confirmed"


# ---------------------------------------------------------------------------
# ATS_CONFIRMED_LABELS - live-derived from the scanner registries (#290).
# ---------------------------------------------------------------------------

# The ATS source-tag vocabulary captured for this analysis (2026-07-16):
# every display-cased `company_source` a direct ATS-scanner sighting has
# written into jobs.sources. This pin is why a scanner REMOVED from the
# registries (or a company_source rename) cannot silently reclassify its
# historical corpus rows to 'unrecognized' - the label moves to
# extract._LEGACY_ATS_LABELS instead of dropping out of the taxonomy.
_SNAPSHOT_ATS_LABELS: frozenset[str] = frozenset(
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


def _live_scanner_labels() -> frozenset[str]:
    return frozenset(
        s.company_source for s in (*SCANNERS_BY_NAME.values(), *PLAYWRIGHT_SCANNERS.values())
    )


def test_ats_confirmed_labels_is_exactly_live_plus_legacy():
    """Derivation contract: the set is exactly the live scanner-label union
    plus the explicit legacy set - a hand-added straggler, or a regression to
    a frozen literal that drifts from the registries, fails here."""
    assert _SCANNER_ATS_LABELS == _live_scanner_labels()
    assert ATS_CONFIRMED_LABELS == _live_scanner_labels() | _LEGACY_ATS_LABELS


def test_ats_confirmed_labels_retains_snapshot_vocabulary():
    """The captured vocabulary never shrinks: a scanner leaving the
    registries must have its label moved to _LEGACY_ATS_LABELS so historical
    corpus rows keep classifying ats_confirmed (the thordata precedent)."""
    assert _SNAPSHOT_ATS_LABELS <= ATS_CONFIRMED_LABELS


def test_every_live_scanner_label_classifies_ats_confirmed():
    """End-to-end: every label the scanners can currently write lands in the
    ats_confirmed bucket, including the display-name variants
    ('Microsoft Careers', 'Oracle Cloud') that are not PLATFORMS keys."""
    for label in _live_scanner_labels():
        assert classify_source([label]) == "ats_confirmed", label


# ---------------------------------------------------------------------------
# load_provenance_records / load_exclusion_counts - fixture-DB integration.
# ---------------------------------------------------------------------------

JOBS = [
    {
        "dedup_key": "a",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-11T00:00:00",
        "expiry_status": "expired",
        "sources": '["Greenhouse"]',
        "is_stale": 0,
        "jd_full": "full description text",
        "sub_scores_json": '{"title_fit": 4}',
    },
    {
        "dedup_key": "b",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-06T00:00:00",
        "expiry_status": "live",
        "sources": '["portal_jooble"]',
        "is_stale": 1,
        "jd_full": None,
        "sub_scores_json": None,
    },
    {
        "dedup_key": "c",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "inconclusive",
        "sources": '["linkedin"]',
        "is_stale": 0,
        "jd_full": "text",
        "sub_scores_json": None,
    },
    {
        # unattributed - excluded from load_provenance_records, counted in exclusions
        "dedup_key": "d",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": None,
        "sources": "[]",
        "is_stale": 0,
        "jd_full": None,
        "sub_scores_json": None,
    },
    {
        # unrecognized - excluded, counted separately
        "dedup_key": "e",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "live",
        "sources": '["some_brand_new_source"]',
        "is_stale": 0,
        "jd_full": None,
        "sub_scores_json": None,
    },
    {
        # malformed sources JSON - excluded at the SQL level
        "dedup_key": "f",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "live",
        "sources": "not valid json",
        "is_stale": 0,
        "jd_full": None,
        "sub_scores_json": None,
    },
]


def _con(tmp_path):
    return open_readonly(build_fixture_db(tmp_path / "f.db", JOBS, COMPANIES))


def test_load_provenance_records_classifies_and_excludes(tmp_path):
    df = _con(tmp_path)
    result = load_provenance_records(df)
    # 6 total rows; d (unattributed), e (unrecognized), and f (malformed JSON)
    # are all excluded here - malformed JSON must not crash the loader, and
    # must not silently count as an empty/unattributed source list either.
    assert set(result["bucket"]) <= {"ats_confirmed", "aggregator_only", "email_alert_only"}
    assert len(result) == 3
    row_a = result[result["bucket"] == "ats_confirmed"].iloc[0]
    assert row_a["has_jd"] is True or bool(row_a["has_jd"])
    assert bool(row_a["is_scored"])
    row_b = result[result["bucket"] == "aggregator_only"].iloc[0]
    assert bool(row_b["is_stale"])
    assert row_b["expiry_status"] == "live"


def test_load_provenance_records_exclude_source_reclassifies(tmp_path):
    con = _con(tmp_path)
    result = load_provenance_records(con, exclude_source="portal_jooble")
    # job b's only source was portal_jooble -> now unattributed -> dropped
    assert len(result) == 2
    assert "aggregator_only" not in set(result["bucket"])


def test_exclusion_counts(tmp_path):
    con = open_readonly(build_fixture_db(tmp_path / "g.db", JOBS, COMPANIES))
    counts = load_exclusion_counts(con)
    assert counts["total"] == 6
    assert counts["malformed_sources_json"] == 1  # f
    assert counts["no_attributable_source"] == 1  # d
    assert counts["unrecognized_source_tag"] == 1  # e
    assert counts["usable"] == 3  # a, b, c
