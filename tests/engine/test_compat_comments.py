"""DB-free regression tests for #388: qmark_to_format must not translate a
`?` that lives inside a SQL comment.

SQLite (and every other SQL dialect) treats a `?` inside a `--` line comment
or `/* */` block comment as inert text, never a placeholder. Before this
fix, `qmark_to_format` walked the whole string outside single-quoted string
literals and blindly rewrote every `?` to `%s`, including ones inside
comments — so a comment that happened to quote qmark SQL (a `PORT-SEAM`
note, e.g.) silently added a placeholder psycopg's client-side `%s` scan
would then count, without a matching bound parameter, raising
`ProgrammingError: ... N placeholders but M parameters` at execute time.

These tests exercise `jobcannon.db.compat` directly — no sqlite3 connection,
no Postgres, no Flask app — matching tests/host/test_compat.py's existing
coverage of the string-literal case but for the comment case this issue
adds.
"""

from __future__ import annotations

from jobcannon.db.compat import engine_sql_to_host, qmark_to_format


def test_qmark_in_line_comment_is_not_translated():
    sql = "SELECT 1 FROM postings WHERE id = ? -- datetime('now', ? || ' days')"
    out = qmark_to_format(sql)
    assert out == "SELECT 1 FROM postings WHERE id = %s -- datetime('now', ? || ' days')"
    assert out.count("%s") == 1


def test_qmark_in_block_comment_is_not_translated():
    sql = "SELECT 1 FROM postings /* seam note: WHERE dedup_key = ? */ WHERE id = ?"
    out = qmark_to_format(sql)
    assert out == "SELECT 1 FROM postings /* seam note: WHERE dedup_key = ? */ WHERE id = %s"
    assert out.count("%s") == 1


def test_double_dash_inside_string_literal_is_not_a_comment():
    # A '--' inside a single-quoted string literal is ordinary string
    # content, not a comment start — the '?' that follows it is real code
    # and must still be translated (this is the inverse of #388: getting
    # comment-detection wrong here would UNDER-translate placeholders).
    sql = "SELECT 1 FROM postings WHERE title = 'a -- b' AND id = ?"
    out = qmark_to_format(sql)
    assert out == "SELECT 1 FROM postings WHERE title = 'a -- b' AND id = %s"
    assert out.count("%s") == 1


def test_percent_in_comments_is_still_escaped():
    # psycopg's %s substitution scans the whole query text, comments
    # included, so a bare '%' left un-escaped inside a comment would still
    # be misread as a placeholder introducer even though '?' there is inert.
    sql = "SELECT 1 FROM postings -- 50% done, see also /* WIP: 90% */ ? "
    out = qmark_to_format(sql)
    assert "%%" in out
    assert "%s" not in out  # the trailing '?' is inside the line comment


def test_sabotage_mixed_placeholder_count_end_to_end():
    # Sabotage-style assertion (per the issue): a realistic mixed sample
    # with placeholders in code, a string literal, a line comment, and a
    # block comment must translate to exactly the number of REAL
    # placeholders — not the number of literal '?' characters in the text.
    sql = (
        "-- eligibility: consecutive_empty_scans <= ?\n"
        "UPDATE jobs SET comp_data_json = ?, note = 'what?' "
        "/* legacy seam: WHERE dedup_key = ? */ "
        "WHERE dedup_key = ? AND last_scanned_at < datetime('now', '-' || ? || ' days')"
    )
    out = engine_sql_to_host(sql)
    # Real placeholders, left to right: comp_data_json=?, dedup_key=?,
    # datetime interval=? -> exactly 3, none contributed by the line
    # comment, the string literal's literal '?', or the block comment.
    assert out.count("%s") == 3
    assert "consecutive_empty_scans <= ?" not in out  # the comment is stripped, not just skipped
    assert "legacy seam" not in out
    assert "'what?'" in out  # string-literal '?' survives untouched
    assert "UPDATE postings" in out and "UPDATE jobs" not in out


def test_engine_sql_to_host_strips_comments_before_datetime_rewrite():
    # A comment that quotes the SQLite datetime() shape as documentation
    # must not get rewritten to Postgres syntax by _DATETIME_REWRITES — the
    # single strip-comments step in engine_sql_to_host runs before any
    # regex rewrite, not just before qmark_to_format.
    sql = "-- see datetime('now') for the retry gate\nSELECT 1 FROM postings WHERE id = ?"
    out = engine_sql_to_host(sql)
    assert "datetime(" not in out
    assert "now()" not in out
    assert out.count("%s") == 1


def test_engine_sql_to_host_strips_comments_before_table_rewrite():
    # Same guarantee for the jobs -> postings table rewrite: a comment
    # mentioning "FROM jobs" as prose must not be rewritten.
    sql = "-- ported from FROM jobs WHERE dedup_key = ?\nSELECT 1 FROM postings WHERE id = ?"
    out = engine_sql_to_host(sql)
    assert "FROM jobs" not in out
    assert out.count("%s") == 1
