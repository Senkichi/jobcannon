from analyses.common.db import open_readonly
from analyses.posting_lifespan.extract import load_exclusion_counts, load_lifespan_records
from tests.fixtures import build_fixture_db

COMPANIES = [
    {"id": 1, "name": "A", "ats_platform": "greenhouse"},
    {"id": 2, "name": "B", "ats_platform": None},
]
JOBS = [
    # expired after 10 days -> observed event, duration 10.0
    {
        "dedup_key": "a",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-11T00:00:00",
        "expiry_status": "expired",
    },
    # still live after 5 days -> censored
    {
        "dedup_key": "b",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-06T00:00:00",
        "expiry_status": "live",
    },
    # inconclusive -> excluded from primary, censored in robustness variant
    {
        "dedup_key": "c",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "inconclusive",
    },
    # epoch-garbage first_seen -> excluded
    {
        "dedup_key": "d",
        "company_id": 1,
        "first_seen": "1970-01-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "expired",
    },
    # null platform -> excluded
    {
        "dedup_key": "e",
        "company_id": 2,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "expired",
    },
    # no company join -> excluded
    {
        "dedup_key": "f",
        "company_id": None,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-04T00:00:00",
        "expiry_status": "expired",
    },
]


def _con(tmp_path):
    return open_readonly(build_fixture_db(tmp_path / "f.db", JOBS, COMPANIES))


def test_primary_records(tmp_path):
    df = load_lifespan_records(_con(tmp_path))
    assert set(df["platform"]) == {"greenhouse"}
    assert len(df) == 2  # a (event) + b (censored)
    a = df[df["duration_days"] == 10.0]
    assert a["observed"].tolist() == [1]
    b = df[df["duration_days"] == 5.0]
    assert b["observed"].tolist() == [0]


def test_inconclusive_as_censored_variant(tmp_path):
    df = load_lifespan_records(_con(tmp_path), include_inconclusive_as_censored=True)
    assert len(df) == 3
    c = df[df["duration_days"] == 3.0]
    assert c["observed"].tolist() == [0]


def test_mixed_tz_aware_naive_timestamps(tmp_path):
    # Live corpus has a small number of rows where one of first_seen/last_seen
    # carries a UTC offset (historical write-path bug) while its pair is naive.
    # Must normalize instead of raising on aware-vs-naive subtraction.
    jobs = [
        {
            "dedup_key": "g",
            "company_id": 1,
            "first_seen": "2026-06-01T00:00:00",
            "last_seen": "2026-06-08T00:00:00+00:00",
            "expiry_status": "expired",
        },
    ]
    con = open_readonly(build_fixture_db(tmp_path / "g.db", jobs, COMPANIES))
    df = load_lifespan_records(con)
    assert len(df) == 1
    assert df["duration_days"].tolist() == [7.0]
    assert df["observed"].tolist() == [1]


def test_exclusion_counts(tmp_path):
    counts = load_exclusion_counts(_con(tmp_path))
    assert counts["total"] == 6
    assert counts["no_company_join"] == 1
    assert counts["null_platform"] == 1
    assert counts["bad_first_seen"] == 1
    assert counts["inconclusive_or_null_expiry"] == 1
    assert counts["usable"] == 2
