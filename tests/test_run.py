from analyses.posting_lifespan.run import main
from tests.fixtures import build_fixture_db

COMPANIES = [{"id": 1, "name": "A", "ats_platform": "greenhouse"}]
JOBS = [
    {
        "dedup_key": f"k{i}",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-11T00:00:00",
        "expiry_status": "expired",
    }
    for i in range(250)
]


def test_main_end_to_end(tmp_path, monkeypatch):
    db = build_fixture_db(tmp_path / "f.db", JOBS, COMPANIES)
    monkeypatch.setenv("JOBCANNON_SOURCE_DB", str(db))
    outdir = main(outdir=tmp_path / "out")
    assert (outdir / "NUMBERS.md").is_file()
    assert (outdir / "aggregates.csv").is_file()
    assert (outdir / "figures" / "km_by_platform.png").is_file()
