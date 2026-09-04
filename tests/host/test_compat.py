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
