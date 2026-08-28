"""#235: `unresolved_reasons` ownership-partition race between `upsert_job`'s
UPDATE branch (`_jobs.py`) and `_jd_full.py`'s `set_jd_full` /
`_record_jd_content_reject`.

Both are production writers of `postings.unresolved_reasons` (grep-confirmed
the only two -- `_unresolved_reasons.py` is a Python reference no production
path calls, per its own docstring). `_jd_full.py` owns `JD_CONTENT_REASON_CODES`
(`jd_full_offsite` / `jd_full_expired` / `jd_full_truncated`) via atomic SQL
expressions evaluated against the row's LIVE value (#217/#232).
`ParsedJob.from_job` can independently re-derive those SAME codes from a
DIFFERENT observation of the row's `jd_full` (I-18, its own ingestion's
snippet) -- before this fix, `upsert_job`'s UPDATE wrote that independently-
derived list as a wholesale literal, silently erasing whatever `_jd_full.py`
had most recently committed for those codes.

Tests 1-2 below are DETERMINISTIC two-connection lost-update proofs, modeled
on `tests/host/test_jd_full.py::test_record_jd_content_reject_concurrent_appends_both_survive`
(#217): one connection's write is paused (via a patched `execute`) AFTER it
executes but BEFORE it commits, so it holds the row lock; the other
connection's write on the SAME row genuinely BLOCKS on that lock (not merely
races on timing) until the first releases, then re-evaluates its SQL
expression against the now-committed live row. Tests 3-6 are single-
connection unit coverage of the SQL expression's branches (control /
edge cases), using the rollback-isolated `db_conn` fixture.
"""

from __future__ import annotations

import threading

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _parsed_job(**kwargs):
    from jobcannon.engine.parsed_job import ParsedJob

    kwargs.setdefault("title", "Staff Data Engineer")
    kwargs.setdefault("company", "Acme")
    return ParsedJob(**kwargs)


@pytest.fixture()
def company_id(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('acme')")
    return db_conn.execute("SELECT id FROM companies WHERE name='acme'").fetchone()["id"]


def _svc_conn(conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(conn)


# ---------------------------------------------------------------------------
# Tests 1-2: deterministic two-connection interleaving proofs
# ---------------------------------------------------------------------------


def test_jd_quarantine_survives_concurrent_upsert_job(postgres_test_dsn):
    """JD path commits a quarantine code FIRST; `upsert_job` for the SAME
    row runs concurrently with unrelated parser reasons -> the quarantine
    code must SURVIVE and only the parser-owned codes are replaced.

    Sabotage-verified (see PR body): reverting the SQL CASE expression in
    `_jobs.py`'s UPDATE back to a wholesale `unresolved_reasons = %s` bound
    to `Jsonb(list(parsed.unresolved_reasons) if canonical_changed else ...)`
    makes this test fail -- B's UPDATE (paused-then-unblocked AFTER A
    commits) overwrites the column with `["salary_implausible"]` only,
    erasing A's committed `jd_full_truncated`. Failing assertion:
    `assert set(row["unresolved_reasons"]) == {"jd_full_truncated",
    "salary_implausible"}` observes only `{"salary_implausible"}`.
    """
    from jobcannon.db._jd_full import _record_jd_content_reject
    from jobcannon.db._jobs import upsert_job

    dedup_key = "reason-owners-a-co|staff data engineer"
    conn_a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    conn_b = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    entered = threading.Event()
    release = threading.Event()
    b_committed = threading.Event()
    thread_a: threading.Thread | None = None
    thread_b: threading.Thread | None = None
    real_execute = conn_a.execute
    try:
        cid = conn_a.execute(
            "INSERT INTO companies (name) VALUES ('reason-owners-a-co') RETURNING id"
        ).fetchone()["id"]
        conn_a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES (%s, %s, 'Staff Data Engineer', 'reason-owners-a-co')",
            (dedup_key, cid),
        )
        conn_a.commit()

        def _execute_then_pause(query, *args, **kwargs):
            cur = real_execute(query, *args, **kwargs)
            if "unresolved_reasons" in query:
                entered.set()
                release.wait(timeout=10)
            return cur

        conn_a.execute = _execute_then_pause

        results: dict = {}

        def _run_a():
            _record_jd_content_reject(conn_a, dedup_key, "jd_full_truncated")
            results["a_done"] = True

        def _run_b():
            parsed = _parsed_job(
                dedup_key=dedup_key,
                description="A sufficiently long description to force a canonical change.",
                unresolved_reasons=["salary_implausible"],
            )
            results["b_result"] = upsert_job(_svc_conn(conn_b), parsed, company_id=cid)
            results["b_done"] = True
            b_committed.set()

        thread_a = threading.Thread(target=_run_a)
        thread_a.start()
        assert entered.wait(timeout=10), "A's UPDATE was never entered"

        thread_b = threading.Thread(target=_run_b)
        thread_b.start()

        # B must genuinely block on A's row lock -- this wait is expected to
        # time out (see test_record_jd_content_reject_concurrent_appends_both_survive
        # for why this is the harmless, expected case, not a failure).
        b_committed.wait(timeout=1.0)

        release.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not thread_a.is_alive(), "JD-path writer did not finish within timeout"
        assert not thread_b.is_alive(), "upsert_job writer did not finish within timeout"
        assert results.get("a_done") is True
        assert results.get("b_done") is True

        row = conn_a.execute(
            "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
        ).fetchone()
        assert set(row["unresolved_reasons"]) == {"jd_full_truncated", "salary_implausible"}

        # UpsertResult.unresolved_reasons (RETURNING-sourced) must also
        # report the honest, actually-persisted merge -- not the raw
        # (unfiltered) `parsed.unresolved_reasons` value.
        b_result = results["b_result"]
        assert b_result.kind == "updated"
        assert set(b_result.unresolved_reasons) == {"jd_full_truncated", "salary_implausible"}
    finally:
        release.set()
        if thread_a is not None:
            thread_a.join(timeout=10)
        if thread_b is not None:
            thread_b.join(timeout=10)
        try:
            try:
                conn_a.rollback()
                conn_a.execute = real_execute
            except Exception:
                pass
            try:
                conn_a.rollback()
                conn_a.execute("DELETE FROM postings WHERE dedup_key = %s", (dedup_key,))
                conn_a.execute("DELETE FROM companies WHERE name = 'reason-owners-a-co'")
                conn_a.commit()
            finally:
                conn_a.close()
        finally:
            conn_b.close()


def test_upsert_job_then_jd_quarantine_both_survive(postgres_test_dsn):
    """Reverse order: `upsert_job` commits parser reasons FIRST (paused,
    holding the lock); the JD path's `_record_jd_content_reject` for the
    SAME row blocks, then appends onto the now-committed live row. Both
    must survive -- proves the new partition doesn't regress the #217 side
    (which was already correct) and that `upsert_job`'s own write is fully
    self-consistent for a later racer to build on.
    """
    from jobcannon.db._jd_full import _record_jd_content_reject
    from jobcannon.db._jobs import upsert_job

    dedup_key = "reason-owners-b-co|staff data engineer"
    conn_a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    conn_b = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    entered = threading.Event()
    release = threading.Event()
    b_committed = threading.Event()
    thread_a: threading.Thread | None = None
    thread_b: threading.Thread | None = None
    real_execute = conn_a.execute
    try:
        cid = conn_a.execute(
            "INSERT INTO companies (name) VALUES ('reason-owners-b-co') RETURNING id"
        ).fetchone()["id"]
        conn_a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES (%s, %s, 'Staff Data Engineer', 'reason-owners-b-co')",
            (dedup_key, cid),
        )
        conn_a.commit()

        def _execute_then_pause(query, *args, **kwargs):
            cur = real_execute(query, *args, **kwargs)
            if "unresolved_reasons" in query:
                entered.set()
                release.wait(timeout=10)
            return cur

        conn_a.execute = _execute_then_pause

        results: dict = {}

        def _run_a():
            parsed = _parsed_job(
                dedup_key=dedup_key,
                description="A sufficiently long description to force a canonical change.",
                unresolved_reasons=["salary_implausible"],
            )
            results["a_result"] = upsert_job(_svc_conn(conn_a), parsed, company_id=cid)
            results["a_done"] = True

        def _run_b():
            _record_jd_content_reject(conn_b, dedup_key, "jd_full_truncated")
            results["b_done"] = True
            b_committed.set()

        thread_a = threading.Thread(target=_run_a)
        thread_a.start()
        assert entered.wait(timeout=10), "upsert_job's UPDATE was never entered"

        thread_b = threading.Thread(target=_run_b)
        thread_b.start()
        b_committed.wait(timeout=1.0)  # expected to time out -- B is blocked

        release.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not thread_a.is_alive(), "upsert_job writer did not finish within timeout"
        assert not thread_b.is_alive(), "JD-path writer did not finish within timeout"
        assert results.get("a_done") is True
        assert results.get("b_done") is True

        row = conn_a.execute(
            "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
        ).fetchone()
        assert set(row["unresolved_reasons"]) == {"jd_full_truncated", "salary_implausible"}
    finally:
        release.set()
        if thread_a is not None:
            thread_a.join(timeout=10)
        if thread_b is not None:
            thread_b.join(timeout=10)
        try:
            try:
                conn_a.rollback()
                conn_a.execute = real_execute
            except Exception:
                pass
            try:
                conn_a.rollback()
                conn_a.execute("DELETE FROM postings WHERE dedup_key = %s", (dedup_key,))
                conn_a.execute("DELETE FROM companies WHERE name = 'reason-owners-b-co'")
                conn_a.commit()
            finally:
                conn_a.close()
        finally:
            conn_b.close()


# ---------------------------------------------------------------------------
# Tests 3-6: single-connection control + SQL-expression edge cases
# ---------------------------------------------------------------------------


def test_control_parser_codes_still_fully_replace_stale_parser_codes(db_conn, company_id):
    """No JD_CONTENT_REASON_CODES involved at all: a canonical change must
    still fully REPLACE the stale parser-owned codes, not merely append to
    them -- the partition must not accidentally turn every canonical update
    into an appender for every code.
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "control-co|staff data engineer"
    p1 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description.",
        unresolved_reasons=["salary_implausible"],
    )
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["title_invalid_shape"],
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"

    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["title_invalid_shape"]
    assert r2.unresolved_reasons == ["title_invalid_shape"]


def test_malformed_live_value_treated_as_empty(db_conn, company_id):
    """Live `unresolved_reasons` is a non-array jsonb value (legacy/corrupt
    data) -- the `jsonb_typeof(...) = 'array'` guard must fall back to an
    empty set for the JD-owned side instead of raising, mirroring
    `_jd_full.py`'s own malformed-value tolerance.
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "malformed-co|staff data engineer"
    p1 = _parsed_job(dedup_key=dedup_key, description="Short description.")
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = 'null'::jsonb WHERE dedup_key = %s",
        (dedup_key,),
    )

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["title_invalid_shape"],
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["title_invalid_shape"]


def test_empty_live_and_empty_parser_reasons_yields_empty_array(db_conn, company_id):
    """NULL/empty on both sides: a canonical change with no reasons on
    either side must land a real, non-NULL `[]` -- never SQL NULL.
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "empty-co|staff data engineer"
    p1 = _parsed_job(dedup_key=dedup_key, description="Short description.")
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"
    assert r1.unresolved_reasons == []

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == []
    assert r2.unresolved_reasons == []


def test_live_jd_owned_codes_preserved_with_empty_parser_reasons(db_conn, company_id):
    """Live row carries JD_CONTENT_REASON_CODES entries; the incoming
    parser reasons are empty -- the JD-owned codes must survive untouched
    (a pure preserve, no parser-side codes to append).
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "jd-only-co|staff data engineer"
    p1 = _parsed_job(dedup_key=dedup_key, description="Short description.")
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb(["jd_full_offsite", "jd_full_truncated"]), dedup_key),
    )

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert set(row["unresolved_reasons"]) == {"jd_full_offsite", "jd_full_truncated"}
    assert set(r2.unresolved_reasons) == {"jd_full_offsite", "jd_full_truncated"}


def test_stray_foreign_code_on_live_row_is_dropped_on_canonical_change(db_conn, company_id):
    """A code on the live row that is neither JD_CONTENT_REASON_CODES-owned
    nor present in the fresh parser output (e.g. a retired/legacy code) is
    dropped on the next canonical-changing update -- documents that the
    partition is a WHITELIST preserve (JD-owned codes only), matching the
    pre-existing full-replacement contract for everything else
    (`test_control_parser_codes_still_fully_replace_stale_parser_codes`),
    not a general "never drop anything" guarantee.
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "stray-co|staff data engineer"
    p1 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description.",
        unresolved_reasons=["some_retired_legacy_code"],
    )
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["salary_implausible"],
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["salary_implausible"]


def test_parser_reasserted_jd_code_is_deduplicated_not_appended(db_conn, company_id):
    """The Python-side pre-strip filter (`parser_reasons` at `_jobs.py`:
    343-345) has no direct test: this exercises it explicitly. The parser
    independently re-derives the SAME JD code the live row already carries
    (I-18 double-derivation) alongside a genuine parser-owned code. If the
    pre-strip filter were removed or inverted, the SQL side's live-JD
    subquery AND the raw (unfiltered) parser list would each contribute a
    copy of `jd_full_truncated`, landing a duplicate -- exactly the #235
    failure shape. A bare `set()` equality assertion would hide that
    duplicate, so both set AND length are asserted here.
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "prestrip-co|staff data engineer"
    p1 = _parsed_job(dedup_key=dedup_key, description="Short description.")
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb(["jd_full_truncated"]), dedup_key),
    )

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["jd_full_truncated", "salary_implausible"],
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"

    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    expected = {"jd_full_truncated", "salary_implausible"}
    assert set(row["unresolved_reasons"]) == expected
    assert len(row["unresolved_reasons"]) == len(expected), (
        f"duplicate JD code landed: {row['unresolved_reasons']!r}"
    )
    assert set(r2.unresolved_reasons) == expected
    assert len(r2.unresolved_reasons) == len(expected)


def test_multiple_live_jd_codes_and_parser_reasons_all_survive(db_conn, company_id):
    """Coverage gap: no existing test combines 2+ live `JD_CONTENT_REASON_CODES`
    entries with non-empty parser-owned reasons in the same update. Live row
    carries both `jd_full_truncated` and `jd_full_offsite`; the parser
    supplies a genuine parser-owned code (`salary_implausible`) plus a third
    JD code (`jd_full_expired`) it independently re-derived, which must be
    stripped. Both live JD codes must survive untouched, the parser-owned
    code must be appended, and the stripped code must be absent.
    """
    from jobcannon.db._jobs import upsert_job

    dedup_key = "multi-jd-co|staff data engineer"
    p1 = _parsed_job(dedup_key=dedup_key, description="Short description.")
    r1 = upsert_job(_svc_conn(db_conn), p1, company_id=company_id)
    assert r1.kind == "inserted"

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb(["jd_full_truncated", "jd_full_offsite"]), dedup_key),
    )

    p2 = _parsed_job(
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["salary_implausible", "jd_full_expired"],
    )
    r2 = upsert_job(_svc_conn(db_conn), p2, company_id=company_id)
    assert r2.kind == "updated"

    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    expected = {"jd_full_truncated", "jd_full_offsite", "salary_implausible"}
    assert set(row["unresolved_reasons"]) == expected
    assert len(row["unresolved_reasons"]) == len(expected)
    assert "jd_full_expired" not in row["unresolved_reasons"]
    assert set(r2.unresolved_reasons) == expected
    assert len(r2.unresolved_reasons) == len(expected)
