from jobcannon.db.compat import qmark_to_format


def test_translates_bare_qmarks():
    assert (
        qmark_to_format("SELECT jd_full FROM postings WHERE dedup_key = ?")
        == "SELECT jd_full FROM postings WHERE dedup_key = %s"
    )


def test_preserves_qmarks_inside_string_literals():
    sql = "SELECT * FROM postings WHERE title = 'what?' AND dedup_key = ?"
    assert qmark_to_format(sql) == "SELECT * FROM postings WHERE title = 'what?' AND dedup_key = %s"


def test_multiple_params():
    assert (
        qmark_to_format("UPDATE t SET a = ?, b = ? WHERE c = ?")
        == "UPDATE t SET a = %s, b = %s WHERE c = %s"
    )


def test_percent_literals_are_escaped():
    # psycopg treats bare % as a placeholder introducer when params are passed;
    # engine SQL with LIKE '%foo%' must survive translation.
    assert qmark_to_format("SELECT 1 FROM t WHERE x LIKE '%rem%' AND y = ?").count("%%rem%%") == 1


def test_engine_table_rewrite():
    from jobcannon.db.compat import engine_sql_to_host

    assert engine_sql_to_host("SELECT jd_full FROM jobs WHERE dedup_key = ?") == (
        "SELECT jd_full FROM postings WHERE dedup_key = %s"
    )
    # host-authored postings SQL passes through untouched
    assert engine_sql_to_host("SELECT 1 FROM postings") == "SELECT 1 FROM postings"


def test_dormancy_interval_rewrite():
    # ats_scanner/_run.py's _dormancy_gate_clause is kept in SQLite dialect on
    # purpose (tests/engine/ exercises it directly against bare sqlite3) —
    # this compat-layer rewrite is the ONLY place it becomes Postgres syntax.
    from jobcannon.db.compat import engine_sql_to_host

    sql = "last_scanned_at < datetime('now', '-' || ? || ' days')"
    out = engine_sql_to_host(sql)
    assert out == "last_scanned_at < now() - make_interval(days => %s)"


def test_bare_datetime_now_rewrite():
    from jobcannon.db.compat import engine_sql_to_host

    assert engine_sql_to_host("retry_after < datetime('now')") == "retry_after < now()"


def test_dormancy_gate_clause_translates_cleanly_end_to_end():
    # Regression guard tying the compat rewrite to the actual engine clause
    # text (not just a hand-written analog) — pins the two together so a
    # future edit to either side that breaks the match is caught here rather
    # than only surfacing as a live Postgres syntax error.
    from jobcannon.db.compat import engine_sql_to_host
    from jobcannon.engine.ats_scanner._run import _dormancy_gate_clause

    out = engine_sql_to_host(_dormancy_gate_clause())
    assert "make_interval" in out
    assert "datetime(" not in out


def test_bare_column_datetime_rewrite():
    # public #386: `datetime(scanned_at)` is a column-normalizing cast, valid
    # only in SQLite (where scanned_at is text-stored ISO8601 — see
    # careers_crawler/_bench_predicate.py). The hosted `scanned_at` column is
    # a real `timestamptz` (m0001), so stripping the cast is correct there.
    from jobcannon.db.compat import engine_sql_to_host

    assert (
        engine_sql_to_host("datetime(scanned_at) < datetime('now', '-' || ? || ' days')")
        == "scanned_at < now() - make_interval(days => %s)"
    )


def test_now_minus_days_helper_output_translates_for_postgres():
    # #401: engine/_sql_dialect.py's sqlite_now_minus_days() is the single
    # emitter for the canonical interval fragment. Pin the emitted text to
    # the _DATETIME_REWRITES[0] contract so an edit to the helper that
    # produces a shape the rewrite does not recognize (e.g. the pre-negated
    # `? || ' days'` variant from #380) fails here instead of as a live
    # Postgres `datetime(...)` undefined-function error.
    from jobcannon.db.compat import engine_sql_to_host
    from jobcannon.engine._sql_dialect import sqlite_now_minus_days

    out = engine_sql_to_host(f"last_scanned_at < {sqlite_now_minus_days()}")
    assert out == "last_scanned_at < now() - make_interval(days => %s)"


def test_now_minus_days_docstring_documents_emitted_shape():
    # The helper's docstring is the single point of truth for the canonical
    # fragment's contract — its Returns line must name the emitted text
    # byte-identically. Stating the pre-negated `? || ' days'` shape there
    # (the #380 defect class the same docstring warns against) is a
    # self-contradiction that invites copy-paste of the forbidden variant.
    import inspect

    from jobcannon.engine._sql_dialect import sqlite_now_minus_days

    returns_line = next(
        line
        for line in inspect.getdoc(sqlite_now_minus_days).splitlines()
        if line.startswith("Returns")
    )
    assert sqlite_now_minus_days() in returns_line


def test_now_minus_days_helper_is_used_by_all_canonical_shape_sites():
    # #401 adoption guard: every engine site that emits the canonical
    # `datetime('now', '-' || ? || ' days')` interval fragment must do so
    # through sqlite_now_minus_days() — a re-inlined literal (or a drifted
    # variant) at any of these sites reintroduces the duplication the
    # helper exists to eliminate. inspect.getsource on the *function*
    # (not the module) keeps this check scoped to the emitting code.
    import inspect

    from jobcannon.engine import careers_crawler
    from jobcannon.engine.ats_scanner import _run, _scan_log, _scan_selection
    from jobcannon.engine.careers_crawler import _bench_predicate

    for fn in (
        _run._dormancy_gate_clause,
        careers_crawler._lane1_query_sql,
        _bench_predicate.build_bench_predicate_sql,
        _bench_predicate.is_company_benched,
        _scan_selection.prune_selection_log,
        _scan_log.prune_title_outcomes,
    ):
        # Strip the docstring: several of these functions *mention* the
        # helper in their docstrings, and the check must assert the call in
        # executable code, not the prose.
        code = inspect.getsource(fn).replace(fn.__doc__ or "", "")
        assert "sqlite_now_minus_days" in code, (
            f"{fn.__qualname__} no longer emits the interval fragment via "
            "sqlite_now_minus_days() — the canonical shape must come from "
            "engine/_sql_dialect.py, not an inline literal (#401)"
        )


def test_now_minus_days_helper_appears_in_generated_sql():
    # Output-level pin: the builders that emit SQL without a live
    # connection must produce the canonical fragment text, byte-identical
    # to what _DATETIME_REWRITES[0] matches.
    from jobcannon.engine._sql_dialect import sqlite_now_minus_days
    from jobcannon.engine.ats_scanner import _run
    from jobcannon.engine.careers_crawler import _lane1_query_sql
    from jobcannon.engine.careers_crawler._bench_predicate import build_bench_predicate_sql

    fragment = sqlite_now_minus_days()
    assert fragment in _run._dormancy_gate_clause()
    assert fragment in build_bench_predicate_sql()[0]
    assert fragment in _lane1_query_sql("c.id", "TRUE")


def test_bench_predicate_sql_translates_cleanly_end_to_end():
    # Regression guard tying the compat rewrite to the actual bench-predicate
    # SQL text (not a hand-written analog) — same rationale as
    # test_dormancy_gate_clause_translates_cleanly_end_to_end above, and the
    # reason tests/host/test_crawl_batch_pg_predicates.py can run the real
    # crawl_careers_batch lane queries (SELECT list + bench predicate) against
    # a live Postgres connection instead of a "TRUE" stub (#380 review round
    # 1, finding B1).
    from jobcannon.db.compat import engine_sql_to_host
    from jobcannon.engine.careers_crawler._bench_predicate import build_bench_predicate_sql

    sql, params = build_bench_predicate_sql(21)
    out = engine_sql_to_host(sql)
    assert "datetime(" not in out
    assert "make_interval" in out
    assert out.count("%s") == len(params) == 1
