import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def test_insert_then_monotonic_upgrade(db_conn):
    from jobcannon.db._companies import upsert_company

    conn = _svc_conn(db_conn)
    cid = upsert_company(
        conn, "Acme Robotics", ats_platform="lever", ats_slug="acme", ats_probe_status="hit"
    )
    assert isinstance(cid, int)
    # A later 'pending' sighting must NOT downgrade hit.
    cid2 = upsert_company(conn, "Acme Robotics", ats_probe_status="pending")
    assert cid2 == cid
    row = db_conn.execute(
        "SELECT ats_probe_status, ats_platform FROM companies WHERE id = %s", (cid,)
    ).fetchone()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "lever"


def test_slug_collision_leaves_ats_fields_untouched(db_conn):
    from jobcannon.db._companies import upsert_company

    conn = _svc_conn(db_conn)
    upsert_company(
        conn, "First Co", ats_platform="greenhouse", ats_slug="shared", ats_probe_status="hit"
    )
    cid2 = upsert_company(
        conn, "Second Co", ats_platform="greenhouse", ats_slug="shared", ats_probe_status="hit"
    )
    assert isinstance(cid2, int)  # returns the id, does not raise
    row = db_conn.execute("SELECT ats_slug FROM companies WHERE id = %s", (cid2,)).fetchone()
    assert row["ats_slug"] is None  # collision → ATS fields untouched


def test_rejects_nameless_input_with_typed_error(db_conn):
    from jobcannon.db._companies import CompanyNameRejectedError, upsert_company

    conn = _svc_conn(db_conn)
    for bad in ("", "   ", "---"):
        with pytest.raises(CompanyNameRejectedError):
            upsert_company(conn, bad)


def test_accepts_non_latin_company_name(db_conn):
    from jobcannon.db._companies import upsert_company

    assert isinstance(upsert_company(_svc_conn(db_conn), "株式会社テスト"), int)


def test_accepts_digit_only_company_name(db_conn):
    """The predicate is isalnum(), matching the private original — a
    digit-only name is a real company name, not garbage. This is the
    fixture that discriminates isalnum() from the old isalpha() check."""
    from jobcannon.db._companies import upsert_company

    assert isinstance(upsert_company(_svc_conn(db_conn), "1024"), int)


def test_non_name_failure_raises_wrapped_upsert_error(db_conn):
    """ats_probe_status='hit' without platform+slug trips m0001's hit-state
    CHECK — previously swallowed into a silent None, now wrapped and raised
    with the original error chained as __cause__."""
    from jobcannon.db._companies import CompanyUpsertError, upsert_company

    with pytest.raises(CompanyUpsertError) as exc_info:
        upsert_company(_svc_conn(db_conn), "Hit Without Slug", ats_probe_status="hit")
    assert exc_info.value.__cause__ is not None


def test_update_branch_ats_collision_leaves_fields_untouched(db_conn):
    from jobcannon.db._companies import upsert_company

    conn = _svc_conn(db_conn)
    upsert_company(
        conn, "First Co", ats_platform="greenhouse", ats_slug="shared", ats_probe_status="hit"
    )
    cid_b = upsert_company(conn, "Second Co")
    assert isinstance(cid_b, int)
    # Second Co already EXISTS (found by the initial SELECT) — this exercises
    # the UPDATE-branch collision fallback, not the INSERT-branch one covered
    # by test_slug_collision_leaves_ats_fields_untouched above.
    cid_b_again = upsert_company(conn, "Second Co", ats_platform="greenhouse", ats_slug="shared")
    assert cid_b_again == cid_b
    row = db_conn.execute(
        "SELECT ats_platform, ats_slug FROM companies WHERE id = %s", (cid_b,)
    ).fetchone()
    assert row["ats_platform"] is None
    assert row["ats_slug"] is None


def test_case_insensitive_name_dedup(db_conn):
    from jobcannon.db._companies import upsert_company

    conn = _svc_conn(db_conn)
    cid1 = upsert_company(conn, "Acme Robotics")
    cid2 = upsert_company(conn, "ACME ROBOTICS")
    assert cid2 == cid1
    count = db_conn.execute(
        "SELECT count(*) AS n FROM companies WHERE lower(name) = lower('Acme Robotics')"
    ).fetchone()["n"]
    assert count == 1
    row = db_conn.execute("SELECT name FROM companies WHERE id = %s", (cid1,)).fetchone()
    assert row["name"] == "Acme Robotics"  # first-seen casing wins, never renormalized


def test_upsert_company_populates_name_raw(db_conn):
    from jobcannon.db._companies import upsert_company

    cid = upsert_company(db_conn, "Acme Robotics")
    row = db_conn.execute("SELECT name, name_raw FROM companies WHERE id = %s", (cid,)).fetchone()
    assert row["name_raw"] == "Acme Robotics" == row["name"]


def test_reject_reason_attribution(db_conn):
    """Pins each guard's reason literal AND the guard order: 250 dashes is
    both overlong and non-alphanumeric, so the reason must attribute to the
    alphanumeric check, which runs first (same relative order as the private
    boundary's classifier)."""
    from jobcannon.db._companies import CompanyNameRejectedError, upsert_company

    conn = _svc_conn(db_conn)
    with pytest.raises(CompanyNameRejectedError) as exc_info:
        upsert_company(conn, "   ")
    assert exc_info.value.reason == "empty_after_cleanup"

    with pytest.raises(CompanyNameRejectedError) as exc_info:
        upsert_company(conn, "x" * 201)
    assert exc_info.value.reason == "overlong"

    with pytest.raises(CompanyNameRejectedError) as exc_info:
        upsert_company(conn, "-" * 250)
    assert exc_info.value.reason == "no_alphanumeric_characters"
