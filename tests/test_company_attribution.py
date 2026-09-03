# PORTED from tests/test_company_attribution.py @ 7e23f5394b6b278b572971d658f20d4725db3623 (private job-cannon). Ledger L-0484.
"""Tests for ``jobcannon.db._company_attribution.set_company_attribution``.

# PORT-SEAM: private's docstring described careers_scan_enabled=1 /
# careers_crawl_flag_reason=NULL and company_state_history recording -- both
# dropped on this host (see below); rewritten to match.
Covers:
- The invariant bundle (``ats_probe_status='pending'``, ``consecutive_empty_scans=0``,
  ``retry_count=0``, ``retry_after=NULL``, ``miss_reason=NULL``) is applied on
  every call.
- The ``careers_url`` write path sets ``scan_enabled=true`` (this host merges
  private's careers_scan_enabled/ats_scan_enabled split into one column, and
  has no careers_crawl_flag_reason column to clear -- see
  jobcannon/db/_company_attribution.py's own module docstring).
# PORT-SEAM: careers_url bullet rewritten for the scan_enabled collapse.
- The ``_UNSET`` sentinel: fields not passed are left untouched (``None`` clears
  to NULL, ``_UNSET`` preserves the existing value).
- ``AttributionCollisionError`` on UNIQUE(ats_platform, ats_slug) conflict.

# PORT-SEAM: TestSetCompanyAttributionStateHistory (2 tests) dropped entirely
# below -- no company_state_history table on this host (L-0040 ADAPT scope,
# blocked on the WI-13 scan-column split). The migrated_db_mem fixture name is
# deliberately preserved (now wrapping the public Postgres db_conn fixture,
# not a real sqlite3 in-memory migrated DB) to keep every test method's body
# byte-identical to private -- only the fixture definition and the handful of
# schema-shaped literals below actually change.
"""

from __future__ import annotations

# PORT-SEAM: datetime import dropped -- updated_at is server-side now() on this host.

import pytest

from jobcannon.db._company_attribution import AttributionCollisionError, set_company_attribution

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
def migrated_db_mem(db_conn):  # noqa: F811
    return db_conn


def _insert_company(
    conn,
    name="Acme Corp",
    ats_probe_status="pending",
    ats_platform=None,
    ats_slug=None,
    miss_reason=None,
    retry_count=0,
    retry_after=None,
    consecutive_empty_scans=0,
    careers_url=None,
    scan_enabled=True,  # PORT-SEAM: replaces private's careers_scan_enabled/careers_crawl_flag_reason params (dropped, see module docstring)
):
    """Insert a company row with all attribution-relevant fields. Returns id."""
    # PORT-SEAM: RETURNING id / fetchone() replaces sqlite3 cursor.lastrowid;
    # %s replaces ?; updated_at/created_at are server-side defaults (m0001)
    # so no now bind params.
    row = conn.execute(
        # PORT-SEAM: schema-adapted INSERT -- drops ats_scan_enabled/
        # careers_scan_enabled/careers_crawl_flag_reason/created_at/updated_at
        # columns (collapsed/server-defaulted on this host, see module
        # docstring); RETURNING id replaces sqlite3 cursor.lastrowid.
        "INSERT INTO companies "
        "(name, name_raw, ats_platform, ats_slug, "
        "ats_probe_status, scan_enabled, "
        "miss_reason, retry_count, retry_after, "
        "consecutive_empty_scans, careers_url) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (
            name,
            name,
            ats_platform,
            ats_slug,
            ats_probe_status,
            scan_enabled,  # PORT-SEAM: replaces careers_scan_enabled (collapsed column)
            miss_reason,
            retry_count,
            retry_after,
            consecutive_empty_scans,
            careers_url,
            # PORT-SEAM: careers_crawl_flag_reason/now/now params dropped (see above).
        ),
    ).fetchone()  # PORT-SEAM: fetchone() replaces cursor.lastrowid; no commit() (db_conn fixture owns the transaction)
    return row["id"]


def _fetch_company(conn, company_id):
    # PORT-SEAM: ? -> %s (psycopg paramstyle).
    return conn.execute("SELECT * FROM companies WHERE id = %s", (company_id,)).fetchone()


class TestSetCompanyAttributionInvariantBundle:
    """The invariant bundle is applied on every call."""

    def test_clears_stale_miss_reason(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(
            conn,
            ats_probe_status="miss",
            miss_reason="speculative_exhausted",
            retry_count=2,
            retry_after="2026-01-01T00:00:00",
            consecutive_empty_scans=5,
        )
        set_company_attribution(conn, company_id, ats_platform="lever", ats_slug="acme")
        row = _fetch_company(conn, company_id)
        assert row["miss_reason"] is None
        assert row["retry_count"] == 0
        assert row["retry_after"] is None
        assert row["consecutive_empty_scans"] == 0
        assert row["ats_probe_status"] == "pending"

    def test_resets_probe_status_from_hit(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(
            conn, ats_probe_status="hit", ats_platform="greenhouse", ats_slug="old"
        )
        set_company_attribution(conn, company_id, ats_platform="lever", ats_slug="new")
        row = _fetch_company(conn, company_id)
        assert row["ats_probe_status"] == "pending"
        assert row["ats_platform"] == "lever"
        assert row["ats_slug"] == "new"

    def test_resets_probe_status_from_error(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(
            conn,
            ats_probe_status="miss",  # PORT-SEAM: private used 'error'; this host's CHECK constraint only allows pending/hit/miss
            retry_count=3,
            retry_after="2026-01-01T00:00:00",
        )
        set_company_attribution(conn, company_id, ats_platform="ashby", ats_slug="acme")
        row = _fetch_company(conn, company_id)
        assert row["ats_probe_status"] == "pending"
        assert row["retry_count"] == 0
        assert row["retry_after"] is None


class TestSetCompanyAttributionCareersUrl:
    """The careers_url write path applies the reresolve-script semantics."""

    def test_sets_careers_url_and_enables_scan(self, migrated_db_mem):
        conn = migrated_db_mem
        # PORT-SEAM: careers_scan_enabled/careers_crawl_flag_reason kwargs
        # dropped -- collapsed into scan_enabled (see module docstring).
        company_id = _insert_company(
            conn,
            scan_enabled=False,
        )
        set_company_attribution(conn, company_id, careers_url="https://acme.com/careers")
        row = _fetch_company(conn, company_id)
        assert row["careers_url"] == "https://acme.com/careers"
        # PORT-SEAM: careers_crawl_flag_reason assertion dropped -- no column.
        assert row["scan_enabled"] is True

    def test_careers_url_alone_does_not_clobber_ats_fields(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(conn, ats_platform="lever", ats_slug="acme")
        set_company_attribution(conn, company_id, careers_url="https://acme.com/careers")
        row = _fetch_company(conn, company_id)
        # ATS fields preserved — the _UNSET sentinel prevented clobbering.
        assert row["ats_platform"] == "lever"
        assert row["ats_slug"] == "acme"

    def test_careers_url_none_clears_to_null(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(conn, careers_url="https://old.example.com/careers")
        set_company_attribution(conn, company_id, careers_url=None)
        row = _fetch_company(conn, company_id)
        assert row["careers_url"] is None
        # PORT-SEAM: scan_enabled is still set true (the invariant applies when
        # careers_url is explicitly provided, even if the value is None).
        # PORT-SEAM: careers_crawl_flag_reason assertion dropped -- no column.
        assert row["scan_enabled"] is True


class TestSetCompanyAttributionSentinel:
    """_UNSET preserves existing values; None clears to NULL."""

    def test_ats_platform_unset_preserves_existing(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(conn, ats_platform="greenhouse", ats_slug="old")
        set_company_attribution(conn, company_id, ats_slug="new")
        row = _fetch_company(conn, company_id)
        assert row["ats_platform"] == "greenhouse"
        assert row["ats_slug"] == "new"

    def test_ats_platform_none_clears_to_null(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(conn, ats_platform="greenhouse", ats_slug="old")
        set_company_attribution(conn, company_id, ats_platform=None, ats_slug=None)
        row = _fetch_company(conn, company_id)
        assert row["ats_platform"] is None
        assert row["ats_slug"] is None

    def test_careers_url_unset_preserves_existing(self, migrated_db_mem):
        conn = migrated_db_mem
        company_id = _insert_company(conn, careers_url="https://acme.com/careers")
        set_company_attribution(conn, company_id, ats_platform="lever", ats_slug="acme")
        row = _fetch_company(conn, company_id)
        assert row["careers_url"] == "https://acme.com/careers"
        # PORT-SEAM: scan_enabled not touched when careers_url is _UNSET
        assert row["scan_enabled"] is True


class TestSetCompanyAttributionCollision:
    """AttributionCollisionError on UNIQUE(ats_platform, ats_slug) conflict."""

    def test_collision_raises_with_owner_info(self, migrated_db_mem):
        conn = migrated_db_mem
        owner_id = _insert_company(
            conn,
            name="Owner Corp",
            ats_probe_status="hit",
            ats_platform="greenhouse",
            ats_slug="ownerslug",
        )
        loser_id = _insert_company(conn, name="Loser Corp")

        with pytest.raises(AttributionCollisionError) as exc_info:
            # PORT-SEAM: reflowed onto one line by ruff format (was split across
            # 3 lines in private) -- no semantic change.
            set_company_attribution(conn, loser_id, ats_platform="greenhouse", ats_slug="ownerslug")

        assert exc_info.value.owner_id == owner_id
        assert exc_info.value.owner_name == "Owner Corp"
        assert exc_info.value.ats_platform == "greenhouse"
        assert exc_info.value.ats_slug == "ownerslug"

    def test_collision_does_not_commit(self, migrated_db_mem):
        conn = migrated_db_mem
        _insert_company(conn, name="Taken Corp", ats_platform="greenhouse", ats_slug="taken")
        # PORT-SEAM: private's loser started ats_probe_status='hit' with no
        # ats_platform/ats_slug -- this host's CHECK constraint requires both
        # non-null when status='hit' (m0001), so the loser is seeded with its
        # own valid non-colliding pair instead; also given a distinct name
        # (companies.name is UNIQUE on this host).
        loser_id = _insert_company(
            conn,
            name="Loser Corp 2",
            ats_probe_status="hit",
            ats_platform="workday",
            ats_slug="loser-slug",
        )

        with pytest.raises(AttributionCollisionError):
            set_company_attribution(conn, loser_id, ats_platform="greenhouse", ats_slug="taken")

        # Loser's row is unchanged — no commit on the collision path.
        row = _fetch_company(conn, loser_id)
        assert (
            row["ats_platform"] == "workday"
        )  # PORT-SEAM: reflects the loser's own seeded ats fields (see above)
        assert row["ats_slug"] == "loser-slug"
        assert row["ats_probe_status"] == "hit"


# PORT-SEAM: TestSetCompanyAttributionStateHistory (test_records_ats_platform_change,
# test_records_miss_reason_clear) dropped entirely -- no company_state_history
# table on this host, and the changed_by kwarg those tests pass doesn't exist
# in the public set_company_attribution signature (see module docstring).
