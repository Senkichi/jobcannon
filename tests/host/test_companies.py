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


def test_rejects_empty_name(db_conn):
    from jobcannon.db._companies import upsert_company

    assert upsert_company(_svc_conn(db_conn), "") is None
    assert upsert_company(_svc_conn(db_conn), "   ") is None


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
