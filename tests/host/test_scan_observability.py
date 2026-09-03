"""jobcannon.db._scan_observability -- ledger L-0067's portable admin/
observability subset (crawl_latency_sli, target_set_size,
surfaced_concentration, off_platform_miss_log, recent_runs,
distinct_locations). Seed helpers copied from
tests/host/test_user_action_counts.py (same table shapes). Read-only module
-- no writer tests needed here, except for log_run (L-0073's writer, reused
to seed get_recent_runs's fixture rows)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from jobcannon.db._persistence import log_run
from jobcannon.db._scan_observability import (
    _normalized_hhi,
    _shannon_entropy,
    get_crawl_latency_sli,
    get_distinct_locations,
    get_off_platform_miss_log,
    get_recent_runs,
    get_surfaced_concentration,
    get_target_set_size,
)
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _seed_company(conn, name, *, ats_platform="greenhouse", scan_enabled=True):
    # m0001 CHECK: ats_probe_status <> 'hit' OR (ats_platform IS NOT NULL AND ats_slug IS NOT NULL)
    ats_slug = name.lower().replace(" ", "-") if ats_platform is not None else None
    ats_probe_status = "hit" if ats_platform is not None else "pending"
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status, scan_enabled) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (name, name, ats_platform, ats_slug, ats_probe_status, scan_enabled),
    ).fetchone()["id"]


def _seed_posting(
    conn,
    dedup_key,
    company_id=None,
    *,
    company="Acme",
    classification=None,
    sub_scores=None,
    sources=None,
    posted_date=None,
    first_seen=None,
    locations_raw=None,
):
    columns = ["dedup_key", "title", "company"]
    values = [dedup_key, "Engineer", company]
    if company_id is not None:
        columns.append("company_id")
        values.append(company_id)
    if classification is not None:
        columns.extend(["classification", "scoring_model", "scoring_provider"])
        values.extend([classification, "gpt", "openai"])
    if sub_scores is not None:
        import json

        columns.append("sub_scores_json")
        values.append(json.dumps(sub_scores))
    if sources is not None:
        import json

        columns.append("sources")
        values.append(json.dumps(sources))
    if posted_date is not None:
        columns.extend(["posted_date", "posted_date_precision"])
        values.extend([posted_date, "exact"])
    if first_seen is not None:
        columns.append("first_seen")
        values.append(first_seen)
    if locations_raw is not None:
        import json

        columns.append("locations_raw")
        values.append(json.dumps(locations_raw))

    placeholders = ", ".join(["%s"] * len(values))
    cols_sql = ", ".join(columns)
    return conn.execute(
        f"INSERT INTO postings ({cols_sql}) VALUES ({placeholders}) RETURNING id",
        values,
    ).fetchone()["id"]


def _full_scores(mean):
    """All six SUB_SCORE_KEYS set to the same value -> mean == value."""
    return {
        "title_fit": mean,
        "location_fit": mean,
        "comp_fit": mean,
        "domain_match": mean,
        "seniority_match": mean,
        "skills_match": mean,
    }


# --- _normalized_hhi / _shannon_entropy (pure helpers) ---


def test_hhi_zero_total_is_none():
    assert _normalized_hhi([0, 0]) is None


def test_hhi_single_group_is_one():
    assert _normalized_hhi([5]) == 1.0


def test_hhi_even_split_is_zero():
    assert _normalized_hhi([10, 10]) == 0.0


def test_shannon_entropy_single_group_is_none():
    assert _shannon_entropy([5]) is None


def test_shannon_entropy_zero_total_is_none():
    assert _shannon_entropy([0, 0]) is None


# --- get_crawl_latency_sli ---


def test_crawl_latency_sli_computes_percentiles(db_conn):
    company = _seed_company(db_conn, "latency-co")
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for days in (1, 2, 3, 4, 5):
        _seed_posting(
            db_conn,
            f"lat|{days}",
            company,
            posted_date=date(2026, 6, 1),
            first_seen=base + timedelta(days=days),
        )

    result = get_crawl_latency_sli(db_conn, {})

    assert result["sample_n"] == 5
    assert result["total_dated"] == 5
    assert result["p50_days"] == 3.0
    assert result["cold_start_exclude_days"] == 30


def test_crawl_latency_sli_excludes_same_day_copy_artifacts(db_conn):
    company = _seed_company(db_conn, "latency-sameday-co")
    _seed_posting(
        db_conn,
        "lat-same|1",
        company,
        posted_date=date(2026, 6, 1),
        first_seen=datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc),
    )

    result = get_crawl_latency_sli(db_conn, {})

    assert result["sample_n"] == 0
    assert result["total_dated"] == 1


def test_crawl_latency_sli_respects_config_cold_start_days(db_conn):
    company = _seed_company(db_conn, "latency-cfg-co")
    _seed_posting(
        db_conn,
        "lat-cfg|1",
        company,
        posted_date=date(2026, 1, 1),
        first_seen=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    result = get_crawl_latency_sli(
        db_conn, {"metrics": {"crawl_latency": {"cold_start_exclude_days": 5}}}
    )

    assert result["sample_n"] == 0
    assert result["cold_start_exclude_days"] == 5


# --- get_target_set_size ---


def test_target_set_size_counts_scored_above_floor_excluding_hard_negatives(db_conn):
    company = _seed_company(db_conn, "target-co")
    _seed_posting(db_conn, "tgt|1", company, classification="apply", sub_scores=_full_scores(4.5))
    _seed_posting(
        db_conn, "tgt|2", company, classification="consider", sub_scores=_full_scores(2.0)
    )
    _seed_posting(db_conn, "tgt|3", company, classification="reject", sub_scores=_full_scores(4.5))
    _seed_posting(
        db_conn, "tgt|4", company, classification="low_signal", sub_scores=_full_scores(4.5)
    )

    assert get_target_set_size(db_conn, 3.0) == 1


def test_target_set_size_excludes_unscored_postings(db_conn):
    company = _seed_company(db_conn, "target-unscored-co")
    _seed_posting(db_conn, "tgt-un|1", company)

    assert get_target_set_size(db_conn, 1.0) == 0


# --- get_surfaced_concentration ---


def test_surfaced_concentration_unknown_platform_sentinel(db_conn):
    """postings.company_id is NOT NULL on this host (m0001), so the
    '_unlinked' by_employer sentinel is structurally unreachable here (see
    the module's PORT-SEAM note on get_surfaced_concentration) -- this test
    exercises the still-reachable '_unknown' by_platform sentinel instead,
    via a company with no ats_platform set."""
    company = _seed_company(db_conn, "conc-noplat-co", ats_platform=None)
    _seed_posting(db_conn, "conc|1", company, classification="apply")

    result = get_surfaced_concentration(db_conn)

    assert result["by_employer"]["total"] == 1
    assert result["by_employer"]["hhi"] == 1.0
    assert result["by_platform"]["total"] == 1


def test_surfaced_concentration_excludes_non_surfaced_classifications(db_conn):
    company = _seed_company(db_conn, "conc-skip-co")
    _seed_posting(db_conn, "conc-skip|1", company, classification="skip")

    result = get_surfaced_concentration(db_conn)

    assert result["by_employer"]["total"] == 0


def test_surfaced_concentration_hhi_even_split_across_two_employers(db_conn):
    c1 = _seed_company(db_conn, "conc-even-1")
    c2 = _seed_company(db_conn, "conc-even-2")
    _seed_posting(db_conn, "conc-even|1", c1, classification="apply")
    _seed_posting(db_conn, "conc-even|2", c2, classification="consider")

    result = get_surfaced_concentration(db_conn)

    assert result["by_employer"]["n_groups"] == 2
    assert result["by_employer"]["hhi"] == 0.0


# --- get_off_platform_miss_log ---


def test_off_platform_miss_log_buckets_reachable(db_conn):
    company = _seed_company(
        db_conn, "miss-reachable-co", ats_platform="greenhouse", scan_enabled=True
    )
    _seed_posting(db_conn, "miss-r|1", company, sources=["off_platform_email"])

    result = get_off_platform_miss_log(db_conn)

    assert result["total"] == 1
    assert result["reachable"] == 1


def test_off_platform_miss_log_untracked_bucket_unreachable_on_this_host(db_conn):
    """postings.company_id is NOT NULL on this host (m0001), so the
    unreachable_untracked bucket (c.id IS NULL after the LEFT JOIN) is
    structurally unreachable -- see the module's PORT-SEAM note on
    get_off_platform_miss_log. Confirms every off-platform-email posting
    lands in SOME bucket, none in unreachable_untracked, by construction."""
    company = _seed_company(db_conn, "miss-u-co", ats_platform="jobvite", scan_enabled=True)
    _seed_posting(db_conn, "miss-u|1", company, sources=["off_platform_email"])

    result = get_off_platform_miss_log(db_conn)

    assert result["unreachable_untracked"] == 0
    assert result["total"] == 1


def test_off_platform_miss_log_buckets_unsupported_platform(db_conn):
    # "jobvite" is a real ats_platform value used elsewhere in this test
    # suite but is NOT in jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS.
    company = _seed_company(
        db_conn, "miss-unsupported-co", ats_platform="jobvite", scan_enabled=True
    )
    _seed_posting(db_conn, "miss-un|1", company, sources=["off_platform_email"])

    result = get_off_platform_miss_log(db_conn)

    assert result["unreachable_unsupported"] == 1


def test_off_platform_miss_log_buckets_scan_disabled(db_conn):
    company = _seed_company(
        db_conn, "miss-disabled-co", ats_platform="greenhouse", scan_enabled=False
    )
    _seed_posting(db_conn, "miss-d|1", company, sources=["off_platform_email"])

    result = get_off_platform_miss_log(db_conn)

    assert result["unreachable_scan_disabled"] == 1


def test_off_platform_miss_log_ignores_postings_without_the_source_tag(db_conn):
    company = _seed_company(db_conn, "miss-notag-co")
    _seed_posting(db_conn, "miss-nt|1", company, sources=["linkedin"])

    result = get_off_platform_miss_log(db_conn)

    assert result["total"] == 0


def test_off_platform_miss_log_reachable_above_fit_floor(db_conn):
    company = _seed_company(db_conn, "miss-fit-co", ats_platform="greenhouse", scan_enabled=True)
    _seed_posting(
        db_conn,
        "miss-fit|1",
        company,
        sources=["off_platform_email"],
        classification="apply",
        sub_scores=_full_scores(4.5),
    )

    result = get_off_platform_miss_log(db_conn, fit_floor=3.0)

    assert result["reachable_above_fit_floor"] == 1


def test_off_platform_miss_log_reachable_above_fit_floor_none_when_not_supplied(db_conn):
    company = _seed_company(db_conn, "miss-nofit-co", ats_platform="greenhouse", scan_enabled=True)
    _seed_posting(db_conn, "miss-nofit|1", company, sources=["off_platform_email"])

    result = get_off_platform_miss_log(db_conn)

    assert result["reachable_above_fit_floor"] is None


# --- get_recent_runs ---


def test_recent_runs_excludes_maintenance_kind(db_conn):
    log_run(db_conn, "gmail", 5, 2, 1, {"kind": "ingestion"})
    log_run(db_conn, "maintenance", 0, 0, 0, {"kind": "maintenance"})

    runs = get_recent_runs(db_conn)

    assert len(runs) == 1
    assert runs[0]["source"] == "gmail"


def test_recent_runs_includes_rows_with_no_kind_key(db_conn):
    log_run(db_conn, "serpapi", 3, 1, 1)

    runs = get_recent_runs(db_conn)

    assert len(runs) == 1


def test_recent_runs_respects_limit(db_conn):
    for i in range(3):
        log_run(db_conn, f"src{i}", 1, 1, 1)

    runs = get_recent_runs(db_conn, limit=2)

    assert len(runs) == 2


# --- get_distinct_locations ---


def test_distinct_locations_dedupes_case_insensitively(db_conn):
    company = _seed_company(db_conn, "loc-co")
    _seed_posting(db_conn, "loc|1", company, locations_raw=["Austin, TX"])
    _seed_posting(db_conn, "loc|2", company, locations_raw=["austin, tx"])

    locations = get_distinct_locations(db_conn)

    assert len(locations) == 1


def test_distinct_locations_skips_empty_arrays(db_conn):
    company = _seed_company(db_conn, "loc-empty-co")
    _seed_posting(db_conn, "loc-empty|1", company, locations_raw=[])

    locations = get_distinct_locations(db_conn)

    assert locations == []
