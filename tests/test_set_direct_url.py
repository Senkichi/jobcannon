# PORTED from tests/test_set_direct_url.py @ 929e3ad49398f23c4b9e44904f7aeddc62bf6fda (private job-cannon). Ledger L-0510.
"""Unit tests for the set_direct_url gated DB writer."""
# PORT-SEAM: private's sqlite3 migrated_db_path fixture + hand-rolled jobs
# table replaced with the public Postgres db_conn fixture (tests/host/conftest.py)
# against the real postings/companies schema -- jobs -> postings, adds the
# company_id FK postings requires, ? -> %s paramstyle.
#
# Overlap note: tests/host/test_direct_link.py already exercises the same
# set_direct_url precedence scenarios (strict/loose fill, no-downgrade,
# empty-url/invalid-confidence/missing-row no-ops) plus stamp_direct_url_checks,
# which this private suite predates and does not cover. Carried anyway per the
# literal same-relative-path carry rule (lands at tests/, not tests/host/) --
# no re-adjudication authority over the ledger's PORT verdict; flagging the
# redundancy here rather than silently dropping it.

from __future__ import annotations

# PORT-SEAM: sqlite3 import dropped -- psycopg-only host, no dialect branch.

import pytest

from jobcannon.db._direct_link import set_direct_url

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree, so importing them
# into this module's namespace is what makes pytest discover them here.
# db_conn is then re-requested by name in a local fixture below (F811 is a
# pyflakes false positive for this idiom: the "redefinition" is a distinct
# function scope, not a real shadow).
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres


@pytest.fixture()
def conn(db_conn):  # noqa: F811
    # PORT-SEAM: migrated_db_path (sqlite3, hand-rolled jobs table) replaced
    # with db_conn (Postgres) against the real postings/companies schema --
    # jobs -> postings, adds the company_id FK postings requires.
    company_id = db_conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES ('Acme Corp', 'Acme Corp', 'jobvite', 'acme-corp', 'hit') RETURNING id"
    ).fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES ('k', %s, 'Data Scientist', 'Acme Corp')",
        (company_id,),
    )
    # PORT-SEAM: yield c / c.close() dropped -- db_conn's own fixture owns the
    # rollback + connection close; commit is implicit within its transaction.
    return db_conn


def _read(conn):
    r = conn.execute(
        # PORT-SEAM: jobs -> postings
        "SELECT direct_url, direct_url_confidence FROM postings WHERE dedup_key='k'"
    ).fetchone()
    return r["direct_url"], r["direct_url_confidence"]


def test_writes_strict_into_null(conn):
    assert set_direct_url(conn, "k", "https://x/strict", "strict") is True
    assert _read(conn) == ("https://x/strict", "strict")


def test_writes_loose_into_null(conn):
    assert set_direct_url(conn, "k", "https://x/loose", "loose") is True
    assert _read(conn) == ("https://x/loose", "loose")


def test_loose_does_not_overwrite_existing_loose(conn):
    set_direct_url(conn, "k", "https://x/first", "loose")
    assert set_direct_url(conn, "k", "https://x/second", "loose") is False
    assert _read(conn) == ("https://x/first", "loose")


def test_loose_does_not_overwrite_strict(conn):
    set_direct_url(conn, "k", "https://x/strict", "strict")
    assert set_direct_url(conn, "k", "https://x/loose", "loose") is False
    assert _read(conn) == ("https://x/strict", "strict")


def test_strict_upgrades_loose(conn):
    set_direct_url(conn, "k", "https://x/loose", "loose")
    assert set_direct_url(conn, "k", "https://x/strict", "strict") is True
    assert _read(conn) == ("https://x/strict", "strict")


def test_strict_does_not_overwrite_existing_strict(conn):
    set_direct_url(conn, "k", "https://x/first", "strict")
    assert set_direct_url(conn, "k", "https://x/second", "strict") is False
    assert _read(conn) == ("https://x/first", "strict")


def test_rejects_empty_url(conn):
    assert set_direct_url(conn, "k", "", "strict") is False
    assert set_direct_url(conn, "k", None, "strict") is False
    assert _read(conn) == (None, None)


def test_rejects_unknown_confidence(conn):
    assert set_direct_url(conn, "k", "https://x", "bogus") is False
    assert _read(conn) == (None, None)


def test_returns_false_for_missing_row(conn):
    assert set_direct_url(conn, "nope", "https://x", "strict") is False
