"""jobcannon.db._direct_link -- L-0068's no-downgrade direct_url precedence
writer + resolver-attempt bookkeeping. Seed helpers copied from
tests/host/test_user_action_counts.py (same table shapes)."""

from __future__ import annotations

from jobcannon.db._direct_link import set_direct_url, stamp_direct_url_checks

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _seed_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (%s, %s, 'jobvite', %s, 'hit') RETURNING id",
        (name, name, name.lower().replace(" ", "-")),
    ).fetchone()["id"]


def _seed_posting(conn, dedup_key, company_id, *, title="Engineer", company="Acme"):
    return conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (dedup_key, company_id, title, company),
    ).fetchone()["id"]


def _fetch_direct_url_row(conn, dedup_key):
    return conn.execute(
        "SELECT direct_url, direct_url_confidence, direct_url_checked_at, direct_url_attempts "
        "FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()


# --- set_direct_url ---


def test_strict_fills_a_null_slot(db_conn):
    company = _seed_company(db_conn, "dl-strict-fill-co")
    _seed_posting(db_conn, "dl-strict-fill|1", company)

    wrote = set_direct_url(db_conn, "dl-strict-fill|1", "https://co.example/jobs/1", "strict")

    assert wrote is True
    row = _fetch_direct_url_row(db_conn, "dl-strict-fill|1")
    assert row["direct_url"] == "https://co.example/jobs/1"
    assert row["direct_url_confidence"] == "strict"


def test_loose_fills_a_null_slot(db_conn):
    company = _seed_company(db_conn, "dl-loose-fill-co")
    _seed_posting(db_conn, "dl-loose-fill|1", company)

    wrote = set_direct_url(db_conn, "dl-loose-fill|1", "https://co.example/jobs/2", "loose")

    assert wrote is True
    row = _fetch_direct_url_row(db_conn, "dl-loose-fill|1")
    assert row["direct_url_confidence"] == "loose"


def test_strict_upgrades_an_existing_loose_link(db_conn):
    company = _seed_company(db_conn, "dl-upgrade-co")
    _seed_posting(db_conn, "dl-upgrade|1", company)
    set_direct_url(db_conn, "dl-upgrade|1", "https://co.example/loose", "loose")

    wrote = set_direct_url(db_conn, "dl-upgrade|1", "https://co.example/strict", "strict")

    assert wrote is True
    row = _fetch_direct_url_row(db_conn, "dl-upgrade|1")
    assert row["direct_url"] == "https://co.example/strict"
    assert row["direct_url_confidence"] == "strict"


def test_loose_never_overwrites_an_existing_loose_link(db_conn):
    company = _seed_company(db_conn, "dl-loose-stable-co")
    _seed_posting(db_conn, "dl-loose-stable|1", company)
    set_direct_url(db_conn, "dl-loose-stable|1", "https://co.example/first", "loose")

    wrote = set_direct_url(db_conn, "dl-loose-stable|1", "https://co.example/second", "loose")

    assert wrote is False
    row = _fetch_direct_url_row(db_conn, "dl-loose-stable|1")
    assert row["direct_url"] == "https://co.example/first"


def test_loose_never_overwrites_an_existing_strict_link(db_conn):
    company = _seed_company(db_conn, "dl-loose-vs-strict-co")
    _seed_posting(db_conn, "dl-loose-vs-strict|1", company)
    set_direct_url(db_conn, "dl-loose-vs-strict|1", "https://co.example/strict", "strict")

    wrote = set_direct_url(db_conn, "dl-loose-vs-strict|1", "https://co.example/loose", "loose")

    assert wrote is False
    row = _fetch_direct_url_row(db_conn, "dl-loose-vs-strict|1")
    assert row["direct_url"] == "https://co.example/strict"
    assert row["direct_url_confidence"] == "strict"


def test_strict_never_overwrites_an_existing_strict_link(db_conn):
    company = _seed_company(db_conn, "dl-strict-stable-co")
    _seed_posting(db_conn, "dl-strict-stable|1", company)
    set_direct_url(db_conn, "dl-strict-stable|1", "https://co.example/first-strict", "strict")

    wrote = set_direct_url(
        db_conn, "dl-strict-stable|1", "https://co.example/second-strict", "strict"
    )

    assert wrote is False
    row = _fetch_direct_url_row(db_conn, "dl-strict-stable|1")
    assert row["direct_url"] == "https://co.example/first-strict"


def test_empty_url_is_a_noop(db_conn):
    company = _seed_company(db_conn, "dl-empty-co")
    _seed_posting(db_conn, "dl-empty|1", company)

    assert set_direct_url(db_conn, "dl-empty|1", "", "strict") is False
    assert set_direct_url(db_conn, "dl-empty|1", None, "strict") is False


def test_invalid_confidence_is_a_noop(db_conn):
    company = _seed_company(db_conn, "dl-invalid-co")
    _seed_posting(db_conn, "dl-invalid|1", company)

    wrote = set_direct_url(db_conn, "dl-invalid|1", "https://co.example/x", "maybe")

    assert wrote is False


def test_missing_dedup_key_is_a_noop(db_conn):
    wrote = set_direct_url(db_conn, "dl-does-not-exist", "https://co.example/x", "strict")

    assert wrote is False


# --- stamp_direct_url_checks ---


def test_stamp_direct_url_checks_increments_attempts_and_sets_checked_at(db_conn):
    company = _seed_company(db_conn, "dl-stamp-co")
    _seed_posting(db_conn, "dl-stamp|1", company)

    stamp_direct_url_checks(db_conn, ["dl-stamp|1"])

    row = _fetch_direct_url_row(db_conn, "dl-stamp|1")
    assert row["direct_url_attempts"] == 1
    assert row["direct_url_checked_at"] is not None


def test_stamp_direct_url_checks_accumulates_across_calls(db_conn):
    company = _seed_company(db_conn, "dl-stamp-acc-co")
    _seed_posting(db_conn, "dl-stamp-acc|1", company)

    stamp_direct_url_checks(db_conn, ["dl-stamp-acc|1"])
    stamp_direct_url_checks(db_conn, ["dl-stamp-acc|1"])

    row = _fetch_direct_url_row(db_conn, "dl-stamp-acc|1")
    assert row["direct_url_attempts"] == 2


def test_stamp_direct_url_checks_scopes_to_the_given_keys_only(db_conn):
    company = _seed_company(db_conn, "dl-stamp-scope-co")
    _seed_posting(db_conn, "dl-stamp-scope|1", company)
    _seed_posting(db_conn, "dl-stamp-scope|2", company)

    stamp_direct_url_checks(db_conn, ["dl-stamp-scope|1"])

    row1 = _fetch_direct_url_row(db_conn, "dl-stamp-scope|1")
    row2 = _fetch_direct_url_row(db_conn, "dl-stamp-scope|2")
    assert row1["direct_url_attempts"] == 1
    assert row2["direct_url_attempts"] == 0


def test_stamp_direct_url_checks_empty_list_is_a_noop(db_conn):
    # Must not raise (e.g. on an empty ANY(%s) array param).
    stamp_direct_url_checks(db_conn, [])
