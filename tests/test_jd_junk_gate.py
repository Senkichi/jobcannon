# PORTED from tests/test_jd_junk_gate.py @ 929e3ad49398f23c4b9e44904f7aeddc62bf6fda (private job-cannon). Ledger L-0492.
"""Tests for the set_jd_full() content-density gate (Phase 46.03).

Verifies:
  1. Each documented junk prefix causes set_jd_full() to return False and
     leaves jd_full unchanged in the DB.
  2. The length-floor case (text shorter than 200 chars) is also rejected.
  3. A legitimate long JD (≥200 chars, non-junk prefix) returns True and is
     written to the DB.

# PORT-SEAM: overlaps tests/host/test_jd_full.py (pre-existing, much more
# extensive coverage of the same set_jd_full chokepoint including the D5
# verdict-persistence / #184 atomic-write behavior this suite predates).
# Carried anyway per the literal same-relative-path carry rule -- no
# re-adjudication authority over the ledger's PORT verdict; flagging the
# redundancy here rather than silently dropping it.
"""

from __future__ import annotations

# PORT-SEAM: os/sqlite3 imports dropped -- GSD_BACKUP_CONFIRMED env gate
# and sqlite3 typing both private-only, see below.
from collections.abc import Iterator
from typing import Any  # PORT-SEAM: Any replaces sqlite3.Connection in type hints below

import pytest

from jobcannon.db._jd_full import set_jd_full

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest per the codebase's established cross-directory
# fixture-import convention -- no root tests/conftest.py added.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# PORT-SEAM: os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1") dropped --
# private-only local-backup-tooling gate with no hosted counterpart.


def _insert_job(conn: Any, dedup_key: str) -> None:
    """Insert a minimal posting row (+ owning company) with jd_full = NULL."""
    # PORT-SEAM: jobs -> postings, plus the company_id FK postings requires
    # (companies.name is UNIQUE, so dedup_key -- unique per test -- doubles
    # as a collision-free company name); ? -> %s.
    company_id = conn.execute(
        "INSERT INTO companies (name) VALUES (%s) RETURNING id", (dedup_key,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES (%s, %s, %s, %s)",
        (dedup_key, company_id, "Test Job", "TestCo"),
    )


def _read_jd(conn: Any, dedup_key: str) -> str | None:
    # PORT-SEAM: jobs -> postings, ? -> %s.
    row = conn.execute("SELECT jd_full FROM postings WHERE dedup_key = %s", (dedup_key,)).fetchone()
    return row["jd_full"] if row else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(db_conn) -> Iterator[tuple[None, Any]]:
    # PORT-SEAM: migrated_db_path/sqlite3.connect replaced with the shared
    # Postgres db_conn fixture (tests/host/conftest.py); the (path, conn)
    # tuple shape is preserved so every test body's `_, conn = db` stays
    # byte-identical to private -- path is unused on this host (None).
    yield None, db_conn


# ---------------------------------------------------------------------------
# Junk-prefix cases — each should return False, leave jd_full unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Each documented junk prefix, padded to > 200 chars so only the prefix
        # triggers the gate (not the length floor).
        "Sign in to view the full job description" + " x" * 100,
        "Loading..." + " x" * 100,
        "Open roles at Acme Corp — join our team" + " x" * 100,
        "Skip to content\n\nMain content area" + " x" * 100,
        "Cookie Policy\nWe use cookies to improve your experience." + " x" * 100,
        "Privacy Policy\nYour privacy matters to us." + " x" * 100,
        "404 Not Found\nThe page you requested does not exist." + " x" * 100,
    ],
    ids=[
        "sign_in_to_view",
        "loading",
        "open_roles_at_acme",
        "skip_to_content",
        "cookie_policy",
        "privacy_policy",
        "404_not_found",
    ],
)
def test_junk_prefix_rejected(db, text):
    _, conn = db
    dedup_key = "test|junk_prefix"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, text, source="test")

    assert result is False, "set_jd_full should return False for junk prefix"
    assert _read_jd(conn, dedup_key) is None, "jd_full should remain NULL after junk-gated write"


def test_length_floor_rejected(db):
    """A short text (< 200 chars) should be rejected."""
    _, conn = db
    dedup_key = "test|length_floor"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, "Short.", source="test")

    assert result is False, "set_jd_full should return False for short text"
    assert _read_jd(conn, dedup_key) is None, (
        "jd_full should remain NULL after length-floor rejection"
    )


def test_truncated_snippet_ellipsis_rejected(db):
    """A snippet ending in '...' is rejected even if it clears the 200-char floor."""
    _, conn = db
    dedup_key = "test|truncated_ellipsis"
    _insert_job(conn, dedup_key)

    snippet = "A" * 227 + "..."  # 230 chars, trailing ellipsis
    result = set_jd_full(conn, dedup_key, snippet, source="test")

    assert result is False, "set_jd_full should reject a trailing-ellipsis snippet"
    assert _read_jd(conn, dedup_key) is None


def test_truncated_snippet_unicode_ellipsis_rejected(db):
    """A snippet ending in '…' is also rejected."""
    _, conn = db
    dedup_key = "test|truncated_unicode_ellipsis"
    _insert_job(conn, dedup_key)

    snippet = "A" * 250 + "…"  # 251 chars, trailing unicode ellipsis
    result = set_jd_full(conn, dedup_key, snippet, source="test")

    assert result is False, "set_jd_full should reject a trailing-… snippet"
    assert _read_jd(conn, dedup_key) is None


# ---------------------------------------------------------------------------
# Legitimate long JD — should return True and write
# ---------------------------------------------------------------------------


def test_legitimate_jd_written(db):
    """A long, non-junk JD should be written and set_jd_full should return True."""
    _, conn = db
    dedup_key = "test|legitimate_jd"
    _insert_job(conn, dedup_key)

    # Build a ≥200-char JD with a non-junk prefix
    long_jd = (
        "We are seeking a talented Software Engineer to join our growing team. "
        "You will work on distributed systems, mentor junior engineers, and "
        "collaborate with product managers to deliver high-impact features. "
        "Requirements: 5+ years Python, strong system design skills, BS/MS CS."
    )
    assert len(long_jd) >= 200, "test setup: long_jd must be ≥200 chars"

    result = set_jd_full(conn, dedup_key, long_jd, source="test")

    assert result is True, "set_jd_full should return True for a legitimate JD"
    stored = _read_jd(conn, dedup_key)
    assert stored == long_jd, "jd_full should match the written text"


def test_none_text_rejected(db):
    """Passing None should return False without touching the DB."""
    _, conn = db
    dedup_key = "test|none_text"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, None, source="test")

    assert result is False
    assert _read_jd(conn, dedup_key) is None


# ---------------------------------------------------------------------------
# Title cross-field reject (I-17), wired at the storage chokepoint.
#
# A substantial body that shares ZERO of the title's content stems is a
# wrong-page capture. Previously set_jd_full was title-blind, so this only
# fired later in the adjudicator re-sweep; the enrichment write path now passes
# the title so the reject happens before the bad body is ever stored + scored.
# ---------------------------------------------------------------------------

# ≥300 chars, passes every content-only signal (no block/listing/404/expired
# prefix), and mentions none of the "Pediatric Dental Hygienist" stems.
_OFFTOPIC_BODY = (
    "Our distribution center is hiring a warehouse associate to operate "
    "forklifts and pallet jacks across the overnight shift. You will load and "
    "unload trucks, stage outbound freight, scan inventory into the warehouse "
    "system, and keep the loading dock organized and safe. Prior warehouse "
    "experience and a clean safety record are strongly preferred for this role."
)


def test_title_zero_overlap_rejected_when_title_supplied(db):
    """A title-mismatched body is rejected at write time when title is passed."""
    _, conn = db
    dedup_key = "test|title_mismatch"
    _insert_job(conn, dedup_key)
    assert len(_OFFTOPIC_BODY) >= 300, "test setup: body must clear the x-field floor"

    result = set_jd_full(
        conn, dedup_key, _OFFTOPIC_BODY, source="test", title="Pediatric Dental Hygienist"
    )

    assert result is False, "title zero-overlap body must be rejected when title supplied"
    assert _read_jd(conn, dedup_key) is None


def test_same_body_stored_when_title_omitted(db):
    """Back-compat: with no title, the gate degrades to content-only and stores."""
    _, conn = db
    dedup_key = "test|no_title"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, _OFFTOPIC_BODY, source="test")

    assert result is True, "content-only gate (no title) must still store the body"
    assert _read_jd(conn, dedup_key) == _OFFTOPIC_BODY


def test_grounded_title_stored(db):
    """A body grounded in its own title passes the cross-field check and stores."""
    _, conn = db
    dedup_key = "test|grounded"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, _OFFTOPIC_BODY, source="test", title="Forklift Operator")

    assert result is True, "title-grounded body must store"
    assert _read_jd(conn, dedup_key) == _OFFTOPIC_BODY
