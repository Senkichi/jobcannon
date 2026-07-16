import sqlite3
from pathlib import Path

JOBS_DDL = """
CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    company_id INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    expiry_status TEXT
)
"""
COMPANIES_DDL = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    ats_platform TEXT
)
"""


def build_fixture_db(path: Path, jobs: list[dict], companies: list[dict]) -> Path:
    con = sqlite3.connect(path)
    con.execute(JOBS_DDL)
    con.execute(COMPANIES_DDL)
    con.executemany(
        "INSERT INTO jobs VALUES (:dedup_key, :company_id, :first_seen, :last_seen, :expiry_status)",
        jobs,
    )
    con.executemany("INSERT INTO companies VALUES (:id, :name, :ats_platform)", companies)
    con.commit()
    con.close()
    return path
