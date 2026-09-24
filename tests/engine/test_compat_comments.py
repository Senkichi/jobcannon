"""DB-free regression tests for #388 (qmark_to_format must not translate a
`?` that lives inside a SQL comment), #391 (the same scanner must track
double-quoted identifiers, so comment-like text inside `"..."` cannot open
a comment and a `?` inside one is never counted), and #402 (the
`_DATETIME_REWRITES`/`_TABLE_REWRITES` regex passes must likewise refuse a
match that lies wholly inside a `'...'`/`"..."` quoted region — literal
data and identifier text are not SQL syntax).

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


def test_double_dash_inside_quoted_identifier_is_not_a_comment():
    # #391: '--' inside a double-quoted identifier is identifier text, not
    # a line-comment start — the inverse of the single-quoted case above.
    # Before this fix _iter_sql_regions tracked '...' literals but not
    # "..." identifiers, so the '--' inside "a--b" opened a line comment
    # that swallowed the rest of the line, leaving the real '?' a literal
    # and under-counting %s against the caller's params tuple.
    sql = 'SELECT 1 FROM postings WHERE "a--b" = ? AND id = ?'
    out = qmark_to_format(sql)
    assert out == 'SELECT 1 FROM postings WHERE "a--b" = %s AND id = %s'
    assert out.count("%s") == 2


def test_qmark_inside_quoted_identifier_is_not_translated():
    # The other direction of #391: a '?' that is identifier content must
    # not count as a placeholder. '""' is the standard SQL escaped-quote
    # doubling inside a quoted identifier and must not end the region
    # early — "c""d--e" is one identifier, so its '--' still cannot open
    # a comment that would swallow the trailing placeholder.
    sql = 'SELECT 1 FROM postings WHERE "a?b" = ? AND "c""d--e" = ?'
    out = qmark_to_format(sql)
    assert out == 'SELECT 1 FROM postings WHERE "a?b" = %s AND "c""d--e" = %s'
    assert out.count("%s") == 2


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


def test_sabotage_placeholder_count_with_quoted_identifiers():
    # Sabotage-style assertion for #391, mirroring
    # test_sabotage_mixed_placeholder_count_end_to_end: a realistic mixed
    # sample must translate to exactly the number of REAL placeholders.
    # Real placeholders, left to right: comp_data_json=?, "a--b"=?,
    # dedup_key=? -> exactly 3. The '--' inside "a--b" must not open a
    # comment (which would swallow the rest of the line and under-count),
    # the '?' inside "b?c" must not count (over-count), and the '?' inside
    # the trailing line comment stays inert.
    sql = (
        "UPDATE jobs SET comp_data_json = ? "
        'WHERE "a--b" = ? AND "b?c" IS NOT NULL AND dedup_key = ? -- was this ?'
    )
    out = engine_sql_to_host(sql)
    assert out.count("%s") == 3
    assert '"a--b"' in out  # the identifier survives intact, not blanked as a comment
    assert '"b?c"' in out  # identifier '?' survives untouched
    assert "was this ?" not in out  # the real line comment is still stripped
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


def test_table_rewrite_skips_string_literal():
    # #402: 'FROM jobs' inside a '...' string literal is stored data, not a
    # table reference — the rewrite must leave it byte-identical while still
    # rewriting the real FROM-clause occurrence.
    sql = "SELECT note FROM jobs WHERE note = 'legacy rows FROM jobs' AND id = ?"
    out = engine_sql_to_host(sql)
    assert out == "SELECT note FROM postings WHERE note = 'legacy rows FROM jobs' AND id = %s"


def test_table_rewrite_skips_quoted_identifier():
    # #402: 'FROM jobs' inside a "..." quoted identifier is a name, not a
    # table reference.
    sql = 'SELECT id AS "rows FROM jobs" FROM jobs WHERE id = ?'
    out = engine_sql_to_host(sql)
    assert '"rows FROM jobs"' in out
    assert "FROM postings WHERE id = %s" in out


def test_datetime_rewrite_skips_quoted_identifier():
    # #402: `datetime('now')` appearing inside a "..." identifier is
    # identifier text, not a function call — only the code-region occurrence
    # may be rewritten. This is the sharpest case for the full-containment
    # rule: the real match's own `'now'` literal IS a quoted region, so a
    # naive "any overlap" guard would refuse legitimate rewrites too.
    sql = "SELECT datetime('now') AS \"datetime('now')\" FROM jobs WHERE id = ?"
    out = engine_sql_to_host(sql)
    assert out == "SELECT now() AS \"datetime('now')\" FROM postings WHERE id = %s"


def test_sabotage_mixed_quoted_regions_end_to_end():
    # Sabotage-style assertion for #402, mirroring the #388/#391 mixed-count
    # tests above: literal 'FROM jobs' data, a "datetime('now')" identifier,
    # a block comment, and the real rewritable shapes must all survive in
    # one pass — exactly 2 real placeholders, one table rewrite, one
    # datetime rewrite.
    sql = (
        "UPDATE jobs SET note = 'copied: FROM jobs WHERE x = ?', flag = ? "
        "/* see \"datetime('now')\" */ WHERE \"datetime('now')\" IS NULL "
        "AND at < datetime('now', '-' || ? || ' days')"
    )
    out = engine_sql_to_host(sql)
    assert out.count("%s") == 2
    assert "'copied: FROM jobs WHERE x = ?'" in out  # literal survives untouched
    assert "\"datetime('now')\" IS NULL" in out  # identifier survives untouched
    assert "UPDATE postings" in out and "UPDATE jobs SET" not in out
    assert "now() - make_interval(days => %s)" in out
