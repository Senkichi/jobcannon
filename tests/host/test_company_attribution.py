"""Host-dialect tests for jobcannon.db._company_attribution (ledger L-0065)."""

from __future__ import annotations

import pytest

from jobcannon.db._company_attribution import (
    _UNSET,
    AttributionCollisionError,
    set_company_attribution,
)
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def _insert_company(db_conn, name, **kwargs):
    cols = ["name", "name_raw", *kwargs.keys()]
    placeholders = ", ".join(["%s"] * len(cols))
    values = [name, name, *kwargs.values()]
    row = db_conn.execute(
        f"INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        values,
    ).fetchone()
    return row["id"]


def _row(db_conn, cid):
    return db_conn.execute(
        "SELECT ats_platform, ats_slug, careers_url, ats_probe_status, "
        "consecutive_empty_scans, retry_count, retry_after, miss_reason, scan_enabled, "
        "ats_scan_enabled, careers_scan_enabled, careers_crawl_flag_reason "
        "FROM companies WHERE id = %s",
        (cid,),
    ).fetchone()


def test_resets_invariant_bundle(db_conn):
    conn = _svc_conn(db_conn)
    cid = _insert_company(
        db_conn,
        "Acme Attribution",
        ats_probe_status="miss",
        consecutive_empty_scans=3,
        retry_count=2,
        retry_after="2026-01-01T00:00:00Z",
        miss_reason="not_found",
    )
    set_company_attribution(conn, cid, ats_platform="lever", ats_slug="acme")

    row = _row(db_conn, cid)
    assert row["ats_platform"] == "lever"
    assert row["ats_slug"] == "acme"
    assert row["ats_probe_status"] == "pending"
    assert row["consecutive_empty_scans"] == 0
    assert row["retry_count"] == 0
    assert row["retry_after"] is None
    assert row["miss_reason"] is None


def test_unset_fields_left_untouched(db_conn):
    conn = _svc_conn(db_conn)
    cid = _insert_company(
        db_conn,
        "Only Careers",
        ats_platform="greenhouse",
        ats_slug="only",
        ats_scan_enabled=False,
        careers_scan_enabled=False,
    )
    set_company_attribution(conn, cid, careers_url="https://example.com/careers")

    row = _row(db_conn, cid)
    assert row["careers_url"] == "https://example.com/careers"
    # ats_platform/ats_slug were left as _UNSET -- must not be clobbered.
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "only"
    assert row["scan_enabled"] is True
    # WI-13 (#329): the careers_url branch co-writes the careers lane only.
    assert row["careers_scan_enabled"] is True
    assert row["ats_scan_enabled"] is False


def test_careers_url_clears_crawl_flag_reason(db_conn):
    """#397: an explicit careers_url write clears careers_crawl_flag_reason
    (m0028's column; private clears it on the same branch), while an ATS-only
    write leaves the flag untouched."""
    conn = _svc_conn(db_conn)
    cid = _insert_company(
        db_conn,
        "Flagged Careers",
        careers_crawl_flag_reason="aggregator_suspected:test",
    )
    set_company_attribution(conn, cid, careers_url="https://example.com/careers")

    row = _row(db_conn, cid)
    assert row["careers_url"] == "https://example.com/careers"
    assert row["careers_crawl_flag_reason"] is None

    cid2 = _insert_company(
        db_conn,
        "Still Flagged",
        careers_crawl_flag_reason="aggregator_suspected:test",
    )
    set_company_attribution(conn, cid2, ats_platform="lever", ats_slug="still")

    row2 = _row(db_conn, cid2)
    assert row2["careers_crawl_flag_reason"] == "aggregator_suspected:test"


def test_none_clears_column(db_conn):
    conn = _svc_conn(db_conn)
    cid = _insert_company(db_conn, "Clear Me", ats_platform="ashby", ats_slug="clear")
    set_company_attribution(conn, cid, ats_platform=None, ats_slug=None)

    row = _row(db_conn, cid)
    assert row["ats_platform"] is None
    assert row["ats_slug"] is None


def test_collision_raises_and_leaves_row_untouched(db_conn):
    conn = _svc_conn(db_conn)
    _insert_company(db_conn, "Owner Co", ats_platform="workday", ats_slug="shared")
    cid2 = _insert_company(db_conn, "Challenger Co", ats_probe_status="miss", miss_reason="x")

    with pytest.raises(AttributionCollisionError) as exc_info:
        set_company_attribution(conn, cid2, ats_platform="workday", ats_slug="shared")

    err = exc_info.value
    assert err.owner_name == "Owner Co"
    assert err.ats_platform == "workday"
    assert err.ats_slug == "shared"

    # Failed UPDATE must not have landed -- the SAVEPOINT rolled back.
    row = _row(db_conn, cid2)
    assert row["ats_platform"] is None
    assert row["miss_reason"] == "x"


def test_default_is_sentinel_not_none():
    assert set_company_attribution.__kwdefaults__["ats_platform"] is _UNSET
    assert set_company_attribution.__kwdefaults__["ats_slug"] is _UNSET
    assert set_company_attribution.__kwdefaults__["careers_url"] is _UNSET
