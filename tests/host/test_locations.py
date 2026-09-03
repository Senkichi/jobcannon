"""Host-dialect tests for jobcannon.db._locations (ledger L-0072)."""

from __future__ import annotations

from jobcannon.db._locations import (
    apply_location_observation,
    merge_locations_raw,
    merge_locations_structured,
)
from jobcannon.engine.location_canonical import JobLocation
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def _posting(db_conn, jd_full=None):
    db_conn.execute("INSERT INTO companies (name) VALUES ('loc-co')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='loc-co'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full) "
        "VALUES ('loc-co|engineer', %s, 'Engineer', 'loc-co', %s)",
        (cid, jd_full),
    )
    return "loc-co|engineer"


def _row(db_conn, dedup_key):
    return db_conn.execute(
        "SELECT location, locations_raw, locations_structured, workplace_type, "
        "primary_country_code FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()


# --- pure helpers -----------------------------------------------------------


def test_merge_locations_raw_dedupes_case_insensitive():
    result = merge_locations_raw(["Remote"], ["remote", "Austin, TX"])
    assert result == ["Remote", "Austin, TX"]


def test_merge_locations_raw_floats_remote_hybrid_to_front():
    result = merge_locations_raw(["Austin, TX"], ["Hybrid"])
    assert result == ["Hybrid", "Austin, TX"]


def test_merge_locations_raw_does_not_mutate_inputs():
    existing = ["Austin, TX"]
    incoming = ["Remote"]
    merge_locations_raw(existing, incoming)
    assert existing == ["Austin, TX"]
    assert incoming == ["Remote"]


def _loc(city, region_code, country_code, workplace_type):
    return JobLocation(
        city=city,
        region=None,
        region_code=region_code,
        country=None,
        country_code=country_code,
        workplace_type=workplace_type,
        raw=f"{city}, {region_code}",
        unresolved=False,
    )


def test_merge_locations_structured_upgrades_unspecified():
    existing = [_loc("Austin", "TX", "US", "UNSPECIFIED")]
    incoming = [_loc("Austin", "TX", "US", "HYBRID")]
    result = merge_locations_structured(existing, incoming)
    assert len(result) == 1
    assert result[0].workplace_type == "HYBRID"


def test_merge_locations_structured_keeps_first_seen_on_conflict():
    existing = [_loc("Austin", "TX", "US", "REMOTE")]
    incoming = [_loc("Austin", "TX", "US", "ONSITE")]
    result = merge_locations_structured(existing, incoming)
    assert result[0].workplace_type == "REMOTE"


# --- apply_location_observation ---------------------------------------------


def test_apply_location_observation_writes_all_columns(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)

    changed = apply_location_observation(conn, dedup_key, "Austin, TX", source="llm_extract")
    assert changed is True

    row = _row(db_conn, dedup_key)
    assert "Austin, TX" in row["location"]
    assert row["locations_raw"] == ["Austin, TX"]
    assert row["locations_structured"] is not None
    assert row["primary_country_code"] == "US"


def test_apply_location_observation_idempotent_reapply(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)
    apply_location_observation(conn, dedup_key, "Austin, TX", source="llm_extract")

    changed_again = apply_location_observation(conn, dedup_key, "austin, tx", source="llm_extract")
    assert changed_again is False


def test_apply_location_observation_missing_dedup_key_returns_false(db_conn):
    conn = _svc_conn(db_conn)
    assert apply_location_observation(conn, "does-not-exist", "Austin, TX", source="x") is False


def test_apply_location_observation_empty_input_returns_false(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)
    assert apply_location_observation(conn, dedup_key, "", source="x") is False
    assert apply_location_observation(conn, dedup_key, "   ", source="x") is False


def test_apply_location_observation_never_downgrades_workplace_type(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)
    apply_location_observation(conn, dedup_key, "Remote", source="first")
    row = _row(db_conn, dedup_key)
    assert row["workplace_type"] == "REMOTE"

    apply_location_observation(conn, dedup_key, "Austin, TX", source="second")
    row = _row(db_conn, dedup_key)
    # A plain city observation resolves to UNSPECIFIED and must not
    # downgrade the previously-determined REMOTE.
    assert row["workplace_type"] == "REMOTE"
