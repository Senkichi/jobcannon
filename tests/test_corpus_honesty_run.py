from analyses.corpus_honesty.run import main
from tests.fixtures import build_fixture_db

COMPANIES = [{"id": 1, "name": "A", "ats_platform": "greenhouse"}]


def _job(i: int, sources: str, is_stale: int, expiry_status: str) -> dict:
    return {
        "dedup_key": f"k{i}",
        "company_id": 1,
        "first_seen": "2026-06-01T00:00:00",
        "last_seen": "2026-06-11T00:00:00",
        "expiry_status": expiry_status,
        "sources": sources,
        "is_stale": is_stale,
        "jd_full": "some description text",
        "sub_scores_json": '{"title_fit": 4}',
    }


JOBS = (
    [_job(i, '["Greenhouse"]', 0, "expired") for i in range(120)]
    + [_job(1000 + i, '["portal_jooble"]', 1, "live") for i in range(120)]
    + [_job(2000 + i, '["linkedin"]', 0, "live") for i in range(120)]
    + [_job(3000 + i, '["careers_crawl"]', 0, "live") for i in range(120)]
)


def test_main_end_to_end(tmp_path, monkeypatch):
    db = build_fixture_db(tmp_path / "f.db", JOBS, COMPANIES)
    monkeypatch.setenv("JOBCANNON_SOURCE_DB", str(db))
    outdir = main(outdir=tmp_path / "out")
    assert (outdir / "NUMBERS.md").is_file()
    assert (outdir / "aggregates.csv").is_file()
    assert (outdir / "figures" / "stale_rate_by_provenance.png").is_file()

    numbers = (outdir / "NUMBERS.md").read_text(encoding="utf-8")
    assert "ats_confirmed" in numbers
    assert "aggregator_only" in numbers
