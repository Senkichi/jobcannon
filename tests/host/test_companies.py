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
