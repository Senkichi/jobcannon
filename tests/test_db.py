import sqlite3

import pytest

from analyses.common.db import open_readonly, resolve_source_db
from tests.fixtures import build_fixture_db


def test_open_readonly_rejects_writes(tmp_path):
    db = build_fixture_db(tmp_path / "f.db", [], [])
    con = open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO companies VALUES (1, 'x', 'greenhouse')")


def test_resolve_source_db_reads_env(tmp_path, monkeypatch):
    db = build_fixture_db(tmp_path / "f.db", [], [])
    monkeypatch.setenv("JOBCANNON_SOURCE_DB", str(db))
    assert resolve_source_db() == db


def test_resolve_source_db_missing_env(monkeypatch):
    monkeypatch.delenv("JOBCANNON_SOURCE_DB", raising=False)
    with pytest.raises(RuntimeError):
        resolve_source_db()
