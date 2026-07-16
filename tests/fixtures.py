import sqlite3
from pathlib import Path

# Columns beyond the original 5 (dedup_key..expiry_status) were added for the
# corpus_honesty analysis (provenance classification). Existing job dicts that
# omit them (posting_lifespan's tests) get the defaults below merged in, so
# the original fixtures keep working unchanged.
JOBS_DDL = """
CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    company_id INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    expiry_status TEXT,
    sources TEXT DEFAULT '[]',
    is_stale INTEGER DEFAULT 0,
    jd_full TEXT,
    sub_scores_json TEXT
)
"""
COMPANIES_DDL = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    ats_platform TEXT
)
"""

_JOB_DEFAULTS = {"sources": "[]", "is_stale": 0, "jd_full": None, "sub_scores_json": None}


def build_fixture_db(path: Path, jobs: list[dict], companies: list[dict]) -> Path:
    con = sqlite3.connect(path)
    con.execute(JOBS_DDL)
    con.execute(COMPANIES_DDL)
    filled_jobs = [{**_JOB_DEFAULTS, **job} for job in jobs]
    con.executemany(
        "INSERT INTO jobs VALUES (:dedup_key, :company_id, :first_seen, :last_seen, "
        ":expiry_status, :sources, :is_stale, :jd_full, :sub_scores_json)",
        filled_jobs,
    )
    con.executemany("INSERT INTO companies VALUES (:id, :name, :ats_platform)", companies)
    con.commit()
    con.close()
    return path
